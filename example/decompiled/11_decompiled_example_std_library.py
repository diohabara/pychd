import os
import shutil
import glob
import tempfile

cwd = os.getcwd()
print("Current working directory: {0}".format(cwd))

print("Files and directories in the current directory:")
for item in os.listdir(cwd):
    print(item)

new_dir = "example_directory"
os.makedirs(new_dir, exist_ok=True)
print("Created new directory: {0}".format(new_dir))

new_name = "renamed_directory"
os.rename(new_dir, new_name)
print("Renamed directory from {0} to {1}".format(new_dir, new_name))

os.rmdir(new_name)
print("Removed directory: {0}".format(new_name))

src_file = "source.txt"
dst_file = "destination.txt"
shutil.copy(src_file, dst_file)
print("Copied {0} to {1}".format(src_file, dst_file))

new_location = "moved.txt"
shutil.move(dst_file, new_location)
print("Moved {0} to {1}".format(dst_file, new_location))

os.remove(new_location)
print("Removed file: {0}".format(new_location))

print("Python files in the current directory:")
for py_file in glob.glob("*.py"):
    print(py_file)

with tempfile.NamedTemporaryFile("w+t", delete=False) as temp_file:
    temp_file.write("Hello, World!")
    temp_path = temp_file.name
    print("Created temporary file: {0}".format(temp_path))

with open(temp_path, "r") as temp_file:
    content = temp_file.read()
    print("Content of the temporary file: {0}".format(content))

os.remove(temp_path)
print("Removed temporary file: {0}".format(temp_path))
