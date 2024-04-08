file_name = "example.txt"

with open(file_name, "r") as file:
    content = file.read()
    print(f"File content:\n{content}")
file_name = "output.txt"
content = "Hello, World!"

with open(file_name, "w") as file:
    file.write(content)
    print(f"Wrote content to {file_name}")
file_name = "log.txt"
log_entry = "This is a log entry."

with open(file_name, "a") as file:
    file.write(f"{log_entry}\n")
    print(f"Appended log entry to {file_name}")
file_name = "example.txt"

with open(file_name, "r") as file:
    print(f"Reading {file_name} line by line:")
    for line in file:
        print(line.strip())
import json

file_name = "data.json"
data = {"name": "John", "age": 30, "city": "New York"}

# Writing JSON data to a file
with open(file_name, "w") as file:
    json.dump(data, file)
    print(f"Wrote JSON data to {file_name}")

# Reading JSON data from a file
with open(file_name, "r") as file:
    loaded_data = json.load(file)
    print(f"Read JSON data from {file_name}:")
    print(loaded_data)
