"""generate_diagram.py — produce a PlantUML class diagram from classes.json.

Usage:
    python generate_diagram.py -i classes.json [-o diagram.puml] [--private]

Arguments:
    -i / --input    Path to classes.json produced by extract.py   (required)
    -o / --output   Output .puml file path         (default: <input stem>.puml)
    --private       Also render private (-) members (default: public + protected only)
"""

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------

# Symbols used in the JSON (from get_access_symbol in ast_visitor.py)
_PUBLIC = "+"
_PROTECTED = "#"
_PRIVATE = "-"

# PlantUML uses the same UML symbols, so no remapping is needed.


def _is_visible(access: str, show_private: bool) -> bool:
    if access == _PRIVATE:
        return show_private
    return True  # public and protected always shown


# ---------------------------------------------------------------------------
# Name sanitisation
# ---------------------------------------------------------------------------


def _plantuml_id(fqn: str) -> str:
    """Convert a fully-qualified C++ name to a valid PlantUML identifier.

    PlantUML class names cannot contain '::'.  We replace them with '__'
    and use 'as' aliases to keep the display name readable.
    """
    return fqn.replace("::", "__")


def _quote(display_name: str) -> str:
    """Wrap a display name in double-quotes if it contains characters that
    would confuse the PlantUML parser (spaces, colons, angle brackets, etc.).
    """
    if any(c in display_name for c in " ::<>(),"):
        return f'"{display_name}"'
    return display_name


# ---------------------------------------------------------------------------
# Per-class block renderer
# ---------------------------------------------------------------------------


def _render_class(entry: dict, show_private: bool) -> list[str]:
    """Return lines for a single class or struct block."""
    fqn = entry["name"]
    kind = entry.get("type", "class")  # "class" or "struct"
    attributes = entry.get("attributes", {})
    methods = entry.get("methods", {})

    uid = _plantuml_id(fqn)
    display_name = _quote(fqn)

    lines = []

    # PlantUML uses 'class' for both; structs get a <<struct>> stereotype so
    # they are visually distinct without needing a separate keyword.
    if kind == "struct":
        lines.append(f"class {display_name} as {uid} <<struct>> {{")
    else:
        lines.append(f"class {display_name} as {uid} {{")

    # --- Attributes ---
    for attr_name, attr in attributes.items():
        access = attr.get("access", _PUBLIC)
        if not _is_visible(access, show_private):
            continue
        data_type = attr.get("data type", "")
        static = attr.get("static", False)
        # PlantUML: {static} modifier before the access symbol
        static_marker = "{static} " if static else ""
        lines.append(f"    {static_marker}{access}{data_type} {attr_name}")

    # --- Methods ---
    for method_sig, method in methods.items():
        access = method.get("access", _PUBLIC)
        if not _is_visible(access, show_private):
            continue
        return_type = method.get("return type", "void")
        static = method.get("static", False)
        static_marker = "{static} " if static else ""
        lines.append(f"    {static_marker}{access}{return_type} {method_sig}")

    lines.append("}")
    return lines


# ---------------------------------------------------------------------------
# Type string parsing
# ---------------------------------------------------------------------------

import re

# Matches the last C++ qualified name in a type string, ignoring cv-qualifiers,
# pointer/reference decorators, and template brackets.
# Examples:
#   "const App::Provider &"  → "App::Provider"
#   "App::Provider *"        → "App::Provider"
#   "App::Provider"          → "App::Provider"
#   "std::vector<App::Foo>"  → "App::Foo"   (last identifier wins — good enough
#                                             for detecting known-class references)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*")


def _extract_base_type(type_spelling: str) -> str:
    """Return the rightmost fully-qualified identifier in a C++ type spelling.

    This strips cv-qualifiers, pointer/reference symbols and template wrappers
    to get the bare class name that can be looked up in known_fqns.
    """
    candidates = _IDENT_RE.findall(type_spelling)
    # Filter out pure C++ keywords that can appear in type strings.
    _KEYWORDS = {
        "const",
        "volatile",
        "unsigned",
        "signed",
        "long",
        "short",
        "int",
        "char",
        "bool",
        "float",
        "double",
        "void",
        "auto",
    }
    candidates = [c for c in candidates if c not in _KEYWORDS]
    # Return the last match — for "std::vector<App::Provider>" this gives
    # "App::Provider", which is what we want to check against known_fqns.
    return candidates[-1] if candidates else ""




# ---------------------------------------------------------------------------
# Relationship renderer
# ---------------------------------------------------------------------------


def _render_relationships(entries: list[dict], known_fqns: set[str]) -> list[str]:
    """Return PlantUML lines for all relationships between known classes.

    Exactly ONE arrow is emitted per unordered class pair {A, B}, chosen by
    the highest-priority relationship that exists between them:

    Priority (high → low)
    ─────────────────────
    1. Generalization  Child --|> Parent
                       Derived from the 'parent' field.

    2. Aggregation     Holder o-- Held
                       Derived from the 'attributes' section: any attribute
                       whose resolved base type is a known class (regardless
                       of whether it is held by value or by pointer/reference).

    3. Association     Caller --> Target
                       Derived from method 'connections': any call, read,
                       write, reference, or return-type connection to a member
                       of another known class.

    Once a pair is claimed at a given priority level it is never overwritten
    by a lower-priority relationship, and duplicates at the same level are
    also suppressed.  The pair is always stored as a *frozenset* so that
    A→B and B→A are treated as the same pair.
    """
    # claimed_pairs: frozenset({uid_a, uid_b}) → the line already chosen for
    # that pair.  Populated in priority order; lower-priority passes skip any
    # pair already present here.
    claimed_pairs: dict[frozenset, str] = {}

    # ── Pass 1: Generalization ─────────────────────────────────────────────
    for entry in entries:
        src_fqn = entry["name"]
        parent_fqn = entry.get("parent", "")
        if not parent_fqn or parent_fqn not in known_fqns:
            continue
        src_uid    = _plantuml_id(src_fqn)
        parent_uid = _plantuml_id(parent_fqn)
        pair = frozenset({src_uid, parent_uid})
        if pair not in claimed_pairs:
            claimed_pairs[pair] = f"{src_uid} --|> {parent_uid}"

    # ── Pass 2: Aggregation (from attributes / members) ───────────────────
    for entry in entries:
        src_fqn = entry["name"]
        src_uid = _plantuml_id(src_fqn)
        for attr in entry.get("attributes", {}).values():
            type_spelling = attr.get("data type", "")
            base_type = _extract_base_type(type_spelling)
            if not base_type:
                continue
            target_fqn = (
                base_type
                if base_type in known_fqns
                else _resolve_owner(base_type, known_fqns)
            )
            if not target_fqn or target_fqn == src_fqn:
                continue
            target_uid = _plantuml_id(target_fqn)
            pair = frozenset({src_uid, target_uid})
            if pair not in claimed_pairs:
                claimed_pairs[pair] = f"{src_uid} o-- {target_uid}"

    # ── Pass 3: Association (from method connections — call/read/write/reference/return) ──
    for entry in entries:
        src_fqn = entry["name"]
        src_uid = _plantuml_id(src_fqn)
        for method in entry.get("methods", {}).values():
            for conn in method.get("connections", []):
                # All connection types (call, read, write, reference, return)
                # are treated as Association.
                target_fqn = conn.get("target", "")
                target_class_fqn = _resolve_owner(target_fqn, known_fqns)
                if not target_class_fqn or target_class_fqn == src_fqn:
                    continue
                target_uid = _plantuml_id(target_class_fqn)
                pair = frozenset({src_uid, target_uid})
                if pair not in claimed_pairs:
                    claimed_pairs[pair] = f"{src_uid} --> {target_uid}"

    # ── Collect and bucket lines for grouped output ────────────────────────
    generalization_lines = [v for v in claimed_pairs.values() if "--|>" in v]
    aggregation_lines    = [v for v in claimed_pairs.values() if "o--"  in v]
    association_lines    = [v for v in claimed_pairs.values() if "-->" in v and "--|>" not in v and "o--" not in v]

    lines = []
    if generalization_lines:
        lines.append("' generalization")
        lines.extend(generalization_lines)
    if aggregation_lines:
        lines.append("' aggregation")
        lines.extend(aggregation_lines)
    if association_lines:
        lines.append("' association")
        lines.extend(association_lines)
    return lines


def _resolve_owner(fqn: str, known_fqns: set[str]) -> str:
    """Return the longest known class FQN that matches fqn.

    Resolution steps (first match wins):
    1. Direct match — fqn is itself a known FQN.
    2. Prefix strip — remove trailing '::member' segments one at a time
       until a known FQN is found (e.g. "App::Provider::method" → "App::Provider").
    3. Simple-name match — fqn contains no '::' (clang emitted an unqualified
       type name such as "D" instead of "space::D").  Search known_fqns for
       entries whose last '::'-segment equals fqn.  Only used when exactly one
       known class has that simple name, to avoid ambiguous resolution.
    """
    if fqn in known_fqns:
        return fqn
    # Strip trailing '::member' segments until we find a match or run out.
    parts = fqn.split("::")
    for i in range(len(parts) - 1, 0, -1):
        candidate = "::".join(parts[:i])
        if candidate in known_fqns:
            return candidate
    # Fallback: unqualified name — match against the simple class name (last
    # segment) of every known FQN.  Only resolve when the match is unambiguous.
    if "::" not in fqn:
        matches = [k for k in known_fqns if k.split("::")[-1] == fqn]
        if len(matches) == 1:
            return matches[0]
    return ""


# ---------------------------------------------------------------------------
# Namespace grouping
# ---------------------------------------------------------------------------


def _namespace_of(fqn: str) -> str:
    """Return the immediate namespace of a fully-qualified class name, or ''."""
    parts = fqn.split("::")
    return "::".join(parts[:-1]) if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# Main diagram builder
# ---------------------------------------------------------------------------


def build_diagram(entries: list[dict], show_private: bool) -> str:
    """Assemble the complete PlantUML source string."""
    known_fqns = {e["name"] for e in entries}

    # Group classes by namespace for 'package' blocks.
    namespaces: dict[str, list[dict]] = {}
    for entry in entries:
        ns = _namespace_of(entry["name"])
        namespaces.setdefault(ns, []).append(entry)

    lines = ["@startuml", ""]

    # Style tweaks for readability
    lines += [
        "skinparam classAttributeIconSize 0",
        "skinparam classFontStyle bold",
        "hide empty members",
        "",
    ]

    # --- Class blocks, wrapped in namespace packages ---
    for ns, ns_entries in sorted(namespaces.items()):
        if ns:
            ns_id = _plantuml_id(ns)
            lines.append(f"namespace {ns_id} {{")
        for entry in ns_entries:
            for line in _render_class(entry, show_private):
                indent = "    " if ns else ""
                lines.append(f"{indent}{line}")
            if ns:
                lines.append("")
        if ns:
            lines.append("}")
        lines.append("")

    # --- Relationships (outside namespace blocks for clean arrow routing) ---
    rel_lines = _render_relationships(entries, known_fqns)
    if rel_lines:
        lines.append("' --- relationships ---")
        lines.extend(rel_lines)
        lines.append("")

    lines.append("@enduml")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a PlantUML class diagram from classes.json"
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help="Path to directory which contains json files produced by `extract.py`",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="output/classes.puml",
        help="Output file (default: output/classes.puml)",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        default=False,
        help="Include private members in the diagram (default: public + protected only)",
    )
    args = parser.parse_args()

    dpath_input = Path(args.input)
    if not dpath_input.exists():
        print(f"[x] '{dpath_input}' not found!", file=sys.stderr)
        sys.exit(1)

    if not dpath_input.is_dir():
        print(f"[x] '{dpath_input}' is not a directory!", file=sys.stderr)
        sys.exit(1)

    database = []
    for fpath in dpath_input.glob("*.json"):
        with open(fpath, "r") as f:
            try:
                info = json.load(f)
            except json.JSONDecodeError as exc:
                print(f"[x] '{dpath_input}' is not valid JSON: {exc}", file=sys.stderr)
                sys.exit(1)

        if not isinstance(info, dict):
            print(f"[x] {fpath} is invalid!", file=sys.stderr)
            continue

        database.append(info)

    diagram = build_diagram(database, show_private=args.private)

    with open(args.output, "w") as f:
        f.write(diagram)

    print(f"Wrote diagram ({len(database)} class(es)/struct(s)) → {args.output}")


if __name__ == "__main__":
    main()
