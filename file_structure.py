import os

def list_files(startpath, output_file, ignore_dirs=None, ignore_files=None, max_depth=None):
    if ignore_dirs is None:
        ignore_dirs = {'.git', '.idea', 'venv', '__pycache__', 'node_modules', '.pytest_cache'} # Common things to ignore
    if ignore_files is None:
        ignore_files = {'.DS_Store'}

    with open(output_file, 'w', encoding='utf-8') as f:
        for root, dirs, files in os.walk(startpath, topdown=True):
            # --- Calculate depth and apply max_depth ---
            # depth = root.replace(startpath, '').count(os.sep) # More reliable way needed if startpath isn't absolute root prefix
            # A simpler way based on relative path from startpath:
            relative_path = os.path.relpath(root, startpath)
            if relative_path == '.':
                depth = 0
            else:
                depth = relative_path.count(os.sep) + 1

            if max_depth is not None and depth > max_depth:
                dirs[:] = [] # Don't recurse deeper
                files[:] = [] # Don't list files at this level if too deep
                continue # Skip processing this directory further

            # --- Filtering ---
            # Filter ignored directories *in place* so os.walk doesn't traverse them
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            # Filter ignored files
            files = [file for file in files if file not in ignore_files]


            # --- Indentation and Output ---
            level = depth
            indent = ' ' * 4 * level + '|-- ' if level > 0 else ''
            f.write(f"{indent}{os.path.basename(root)}\n") # Write directory name

            sub_indent = ' ' * 4 * (level + 1) + '|-- '
            for file in sorted(files): # Sort files alphabetically
                 f.write(f"{sub_indent}{file}\n")

            # Sort directories for consistent output
            dirs.sort()


# --- Configuration ---
project_root = '.'  # Use '.' for the current directory where the script is run
output_filename = 'project_structure_script.txt'
# Optional: Specify directories/files to ignore, or set max_depth
ignore_directories = {'.git', '.idea', 'venv', '__pycache__', 'dist', 'build', '*.egg-info'}
# ignore_files_list = {'.env'}
# depth_limit = 3 # Example: limit to 3 levels deep

print(f"Generating file structure...")
list_files(project_root, output_filename, ignore_dirs=ignore_directories) #, max_depth=depth_limit)
print(f"File structure saved to {output_filename}")