import re

import clang.cindex


# ---------------------------------------------------------------------------
# Constants for return-type parsing
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*")
_TYPE_KEYWORDS = {
    "const", "volatile", "unsigned", "signed", "long", "short",
    "int", "char", "bool", "float", "double", "void", "auto",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_full_qualified_name(node):
    """Iteratively build the fully qualified name (Namespace::Class::Entity).

    Uses iteration instead of recursion to avoid hitting Python's recursion
    limit on deeply nested namespaces or template instantiations.
    """
    parts = []
    while node is not None and node.kind != clang.cindex.CursorKind.TRANSLATION_UNIT:
        name = node.spelling
        if name:
            parts.append(name)
        node = node.semantic_parent
    return "::".join(reversed(parts))


def get_access_symbol(node):
    mapping = {
        clang.cindex.AccessSpecifier.PUBLIC:    "+",
        clang.cindex.AccessSpecifier.PRIVATE:   "-",
        clang.cindex.AccessSpecifier.PROTECTED: "#",
    }
    return mapping.get(node.access_specifier, "+")


def _cursor_key(cursor):
    """Stable identity key for a cursor.

    cursor.hash is a 32-bit integer derived from source location and can
    collide for distinct nodes at the same location (e.g. implicit casts).
    Using (file, line, col, kind) is unique in practice.
    """
    loc = cursor.location
    return (
        str(loc.file) if loc.file else "",
        loc.line,
        loc.column,
        cursor.kind,
    )


# ---------------------------------------------------------------------------
# Connection classification
# ---------------------------------------------------------------------------

def classify_connection(node, lexical_parent=None):
    """Classify a field/variable access as 'read', 'write', or 'reference'.

    lexical_parent must be passed explicitly — semantic_parent returns the
    enclosing declaration (e.g. the method), not the immediate expression parent.
    """
    if lexical_parent is None:
        return "read"

    try:
        if lexical_parent.kind == clang.cindex.CursorKind.UNARY_OPERATOR:
            tokens = list(lexical_parent.get_tokens())
            if tokens:
                # Only inspect the first token — the operator symbol — to
                # avoid false matches from tokens in sub-expressions.
                first = tokens[0].spelling
                if first == '&':
                    return "reference"
                if first in ("++", "--"):
                    return "write"

        if lexical_parent.kind == clang.cindex.CursorKind.BINARY_OPERATOR:
            tokens = list(lexical_parent.get_tokens())
            assign_ops = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
            if any(t.spelling in assign_ops for t in tokens):
                # Verify node is on the LHS — the first direct child.
                children = list(lexical_parent.get_children())
                if children and children[0].location == node.location:
                    return "write"
    except Exception:
        pass

    if "*" in node.type.spelling or "&" in node.type.spelling:
        return "reference"

    return "read"


# ---------------------------------------------------------------------------
# Method connection extraction
# ---------------------------------------------------------------------------

def extract_method_connections(method_node, namespace_filter, owner_class_fqn=None):
    """Extract all inter-class calls and field accesses within a method body.

    owner_class_fqn must be the FQN of the class that declares this method
    (e.g. "space::D").  It is supplied by the caller rather than re-derived
    from method_node.semantic_parent because, for out-of-line definitions
    (e.g. `inline int D::calc() { ... }` outside the class body), the
    definition cursor's semantic_parent is the enclosing namespace, not the
    class — which would make the intra-class guard fail.
    """
    owner_fqn = owner_class_fqn

    # Single combined pre-pass: build the lexical-parent map AND collect
    # object sub-expression locations to suppress, all in one tree walk.
    #
    # lexical_parent_map: cursor_key -> parent cursor
    #   Needed because semantic_parent returns the enclosing *declaration*,
    #   not the immediate expression parent.
    #
    # object_expr_locations: set of (file, line, col) for nodes that are the
    #   *object* of a MEMBER_REF_EXPR (e.g. `mProvider` in
    #   `mProvider.processData()`). Suppressed to avoid double-counting them
    #   as independent data reads.
    lexical_parent_map = {}
    object_expr_locations = set()

    def walk(node):
        for child in node.get_children():
            lexical_parent_map[_cursor_key(child)] = node

            if node.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR:
                if child.kind in (clang.cindex.CursorKind.DECL_REF_EXPR,
                                  clang.cindex.CursorKind.MEMBER_REF_EXPR):
                    loc = child.location
                    object_expr_locations.add((str(loc.file) if loc.file else "", loc.line, loc.column))

            walk(child)

    walk(method_node)

    def is_object_expr(cursor):
        loc = cursor.location
        return (str(loc.file) if loc.file else "", loc.line, loc.column) in object_expr_locations

    conns = []

    for child in method_node.walk_preorder():
        target_node = None
        conn_type   = None

        # 1. CALL_EXPR — free-function calls only.
        #    Member calls are captured via MEMBER_REF_EXPR below to get the
        #    correct fully-qualified target; CALL_EXPR.referenced is None for them.
        if child.kind == clang.cindex.CursorKind.CALL_EXPR:
            ref = child.referenced
            if ref and ref.kind not in (clang.cindex.CursorKind.CXX_METHOD,
                                        clang.cindex.CursorKind.CONSTRUCTOR):
                target_node = ref
                conn_type   = "call"

        # 2. MEMBER_REF_EXPR — member method calls and field accesses.
        #    Skip nodes that are the object sub-expression of an outer
        #    MEMBER_REF_EXPR (they carry no independent connection meaning).
        elif child.kind == clang.cindex.CursorKind.MEMBER_REF_EXPR:
            if not is_object_expr(child):
                ref = child.referenced
                if ref:
                    if ref.kind in (clang.cindex.CursorKind.CXX_METHOD,
                                    clang.cindex.CursorKind.FUNCTION_DECL):
                        conn_type   = "call"
                        target_node = ref
                    elif ref.kind in (clang.cindex.CursorKind.FIELD_DECL,
                                      clang.cindex.CursorKind.VAR_DECL):
                        lexical_parent = lexical_parent_map.get(_cursor_key(child))
                        conn_type      = classify_connection(child, lexical_parent)
                        target_node    = ref

        # 3. DECL_REF_EXPR — free variables / static fields.
        #    Skip those already covered as the object of a member expression.
        elif child.kind == clang.cindex.CursorKind.DECL_REF_EXPR:
            if not is_object_expr(child):
                ref = child.referenced
                if ref and ref.kind in (clang.cindex.CursorKind.VAR_DECL,
                                        clang.cindex.CursorKind.FIELD_DECL):
                    lexical_parent = lexical_parent_map.get(_cursor_key(child))
                    conn_type      = classify_connection(child, lexical_parent)
                    target_node    = ref

        if target_node and conn_type:
            fqn = get_full_qualified_name(target_node)
            # Skip intra-class connections: a method referencing a member of its
            # own class is not an inter-class dependency and should be omitted.
            #
            # Two cases:
            # a) target FQN has the form "<owner_fqn>::<member>" — qualified
            #    member of this class (method, field, or static var).
            # b) target FQN equals owner_fqn — rare edge case where the target
            #    resolves to the class itself.
            if owner_fqn and (
                fqn == owner_fqn
                or fqn.startswith(owner_fqn + "::")
            ):
                continue
            if fqn and (namespace_filter is None or fqn.startswith(namespace_filter)):
                conns.append({"type": conn_type, "target": fqn})

    # Add return type of the method itself as a "return" connection so that
    # plantuml.py can draw an Association edge to the returned class.
    # We emit ALL identifiers found in the return type spelling (e.g. both
    # "std::unique_ptr" and "Y" from "std::unique_ptr<Y>") and let
    # _resolve_owner() in plantuml.py filter to only known classes.
    # Crucially we do NOT apply namespace_filter here: an unqualified name
    # such as "Y" (from "std::unique_ptr<Y>" when Y lives in the same
    # namespace as the method's owner) would never pass a startswith check,
    # causing the connection to be silently dropped.  Resolution against the
    # known-FQN set in plantuml.py is the correct and sufficient filter.
    ret_type = method_node.result_type.spelling
    if ret_type:
        for candidate in _IDENT_RE.findall(ret_type):
            if candidate in _TYPE_KEYWORDS:
                continue
            # Skip intra-class references.
            if owner_fqn and (candidate == owner_fqn or candidate.startswith(owner_fqn + "::")):
                continue
            conns.append({"type": "return", "target": candidate})

    # Deduplicate while preserving first-seen order.
    seen   = set()
    unique = []
    for c in conns:
        key = (c["type"], c["target"])
        if key not in seen:
            unique.append(c)
            seen.add(key)
    return unique


# ---------------------------------------------------------------------------
# Class / struct extraction
# ---------------------------------------------------------------------------

def extract_class_info(node, classes, namespace_filter=None):
    if node.kind in (clang.cindex.CursorKind.CLASS_DECL,
                     clang.cindex.CursorKind.STRUCT_DECL):
        if node.is_definition():
            fqn = get_full_qualified_name(node)
            if not namespace_filter or fqn.startswith(namespace_filter):
                if fqn not in classes:
                    classes[fqn] = {
                        "name":       fqn,
                        "type":       "class" if node.kind == clang.cindex.CursorKind.CLASS_DECL else "struct",
                        "parent":     "",
                        "attributes": {},
                        "methods":    {},
                        "files":      set(),
                    }

                for child in node.get_children():
                    if child.kind == clang.cindex.CursorKind.CXX_BASE_SPECIFIER:
                        # referenced points to the base class declaration.
                        base = child.referenced
                        if base:
                            classes[fqn]["parent"] = get_full_qualified_name(base)
                    elif child.kind == clang.cindex.CursorKind.FIELD_DECL:
                        classes[fqn]["attributes"][child.spelling] = {
                            "data type": child.type.spelling,
                            "access":    get_access_symbol(child),
                            "static":    False,
                        }
                    elif child.kind == clang.cindex.CursorKind.VAR_DECL:
                        classes[fqn]["attributes"][child.spelling] = {
                            "data type": child.type.spelling,
                            "access":    get_access_symbol(child),
                            "static":    True,
                        }
                    elif child.kind == clang.cindex.CursorKind.CXX_METHOD:
                        # Use displayname as the key to correctly handle overloads:
                        # e.g. "process(int)" vs "process(float)" instead of "process".
                        #
                        # Always resolve to the definition cursor before extracting
                        # connections. When a method is declared inside the class body
                        # but defined out-of-line (e.g. `inline int D::calc() { ... }`
                        # after the class), `child` is the forward declaration and has
                        # no body — walk_preorder() would find nothing. The definition
                        # cursor carries the actual AST for the function body.
                        method_def = child.get_definition() or child
                        classes[fqn]["methods"][child.displayname] = {
                            "return type": child.result_type.spelling,
                            "access":      get_access_symbol(child),
                            "static":      child.is_static_method(),
                            "connections": extract_method_connections(method_def, namespace_filter, owner_class_fqn=fqn),
                        }

                    # Recurse into nested class/struct definitions found directly
                    # inside this class — handled here to avoid the generic
                    # recursion below visiting them a second time.
                    elif child.kind in (clang.cindex.CursorKind.CLASS_DECL,
                                        clang.cindex.CursorKind.STRUCT_DECL):
                        extract_class_info(child, classes, namespace_filter)

                # Children already processed above — skip generic recursion.
                return

    # Generic recursion for non-class nodes (namespaces, TU root, etc.)
    for child in node.get_children():
        extract_class_info(child, classes, namespace_filter)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def process(fpath_source, args, namespace):
    index = clang.cindex.Index.create()
    tu    = index.parse(path=fpath_source, args=args)

    # Surface parse errors so callers are not silently given empty results.
    errors = [d for d in tu.diagnostics
              if d.severity >= clang.cindex.Diagnostic.Error]
    if errors:
        for d in errors:
            print(f"  [!] Parse error: {d.spelling} ({d.location})")

    # Pass 1: extract class/struct declarations.
    classes = {}
    extract_class_info(tu.cursor, classes, namespace)

    # Pass 2: collect which files reference each known class via TYPE_REF.
    _collect_type_refs(tu.cursor, classes, fpath_source)

    # Serialise: convert internal sets to sorted lists and return as a list
    # of dicts (one per class/struct) rather than a dict keyed by FQN.
    result = []
    for entry in classes.values():
        entry["files"] = sorted(entry["files"])
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# TYPE_REF collection
# ---------------------------------------------------------------------------

def _collect_type_refs(tu_cursor, classes, fpath_source):
    """Walk the TU and record fpath_source in the 'files' set of every known
    class/struct that is referenced via a TYPE_REF cursor.

    TYPE_REF appears wherever a type is named in code: variable declarations,
    function parameters, return types, base specifiers, template arguments, etc.
    It is intentionally broader than just 'used in a method body' — any mention
    of the type in this translation unit counts as a file reference.
    """
    for cursor in tu_cursor.walk_preorder():
        if cursor.kind == clang.cindex.CursorKind.TYPE_REF:
            ref = cursor.referenced
            if ref is None:
                continue
            fqn = get_full_qualified_name(ref)
            if fqn in classes:
                classes[fqn]["files"].add(fpath_source)

