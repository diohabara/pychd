import os

# Get the current working directory
cwd = os.getcwd()
print(f"Current working directory: {cwd}")

# List files and directories in the current directory
print("Files and directories in the current directory:")
for item in os.listdir(cwd):
    print(item)

# Create a new directory
new_dir = "example_directory"
os.makedirs(new_dir, exist_ok=True)
print(f"Created new directory: {new_dir}")

# Rename the directory
new_name = "renamed_directory"
os.rename(new_dir, new_name)
print(f"Renamed directory from {new_dir} to {new_name}")

# Remove the directory
os.rmdir(new_name)
print(f"Removed directory: {new_name}")
import shutil

src_file = "source.txt"
dst_file = "destination.txt"

# Copy a file
shutil.copy(src_file, dst_file)
print(f"Copied {src_file} to {dst_file}")

# Move a file
new_location = "moved.txt"
shutil.move(dst_file, new_location)
print(f"Moved {dst_file} to {new_location}")

# Remove a file
os.remove(new_location)
print(f"Removed file: {new_location}")
import glob

# Find all Python files in the current directory
print("Python files in the current directory:")
for py_file in glob.glob("*.py"):
    print(py_file)
import tempfile

# Create a temporary file and write content to it
with tempfile.NamedTemporaryFile(mode="w+t", delete=False) as temp_file:
    temp_file.write("Hello, World!")
    temp_path = temp_file.name
    print(f"Created temporary file: {temp_path}")

# Read the content of the temporary file
with open(temp_path, "r") as temp_file:
    content = temp_file.read()
    print(f"Content of the temporary file: {content}")

# Remove the temporary file
os.remove(temp_path)
print(f"Removed temporary file: {temp_path}")
