import ast_visitor

import argparse
import json
import os
from pathlib import Path

def parse_compile_commands(input_file):
    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' not found.")
        return

    with open(input_file, "r") as f:
        try:
            commands = json.load(f)
        except json.JSONDecodeError:
            print(f"Error: '{input_file}' is not a valid JSON file.")
            return

    print(f"Processing {len(commands)} entries...")
    compile_commands = {}

    for entry in commands:
        print(f"  [+] {entry["file"]}")
        source_file = Path(entry["file"])
        
        # Extract arguments from the database
        # Newer CMake versions use 'arguments' (list), older use 'command' (string)
        if "arguments" in entry:
            full_args = entry["arguments"]
        elif "command" in entry:
            import shlex

            full_args = shlex.split(entry["command"])
        else:
            print(f"  [!] Skipping {source_file.name}: No command or arguments found.")
            continue

        clang_args = []
        i = 1
        while len(full_args) > i:
            if full_args[i] in ["-c", "-o"]:
                i += 1

            else:
                clang_args.append(full_args[i])

            i += 1

        if entry["file"] in compile_commands:
            print(f"[!] Duplicated: `{entry["file"]}`!")
        else:            
            compile_commands[entry["file"]] = clang_args
    
    return compile_commands

def main():
    parser = argparse.ArgumentParser(
        description="Strict Namespace C++ Metadata Extractor"
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help="Path to compile_commands.json (default: current dir)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="output",
        help="Path to compile_commands.json (default: current dir)",
    )
    parser.add_argument("--namespace", help="Filter by namespace prefix", default=None)
    args = parser.parse_args()

    compile_commands = parse_compile_commands(args.input)
    dpath_output = Path(args.output)
    dpath_output.mkdir(parents=True, exist_ok=True)

    # Accumulate results from all source files into a single dict keyed by FQN
    # so that classes seen in multiple TUs are merged (their 'files' lists unioned).
    merged: dict[str, dict] = {}

    for fpath_source in compile_commands:
        entries = ast_visitor.process(fpath_source, compile_commands[fpath_source], args.namespace)
        for entry in entries:
            fqn = entry["name"]
            if fqn not in merged:
                merged[fqn] = entry
            else:
                # Union the files lists from both encounters.
                merged[fqn]["files"] = sorted(
                    set(merged[fqn]["files"]) | set(entry["files"])
                )


    for fqn in merged:
        fpath_output = dpath_output / (fqn.replace("::", ".") + ".json")
        with open(fpath_output, "w") as fw:
            json.dump(merged[fqn], fw, indent=2)

        print(f"  {fqn} => {fpath_output}")

if __name__ == "__main__":
    main()
