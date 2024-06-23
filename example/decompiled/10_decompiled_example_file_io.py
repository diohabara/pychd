file_name = "example.txt"

with open(file_name, "r") as file:
    content = file.read()

print("File content:\n{}".format(content))

try:
    with open("output.txt", "w") as file:
        content = "Hello, World!"
        file.write(content)

    print("Wrote content to {}".format(file_name))
except:
    pass

try:
    with open("log.txt", "a") as file:
        log_entry = "This is a log entry."
        file.write("{}\n".format(log_entry))

    print("Appended log entry to {}".format(file_name))
except:
    pass

file_name = "example.txt"

print("Reading {} line by line:".format(file_name))
with open(file_name, "r") as file:
    for line in file:
        print(line.strip())

import json

file_name = "data.json"
data = {"name": "John", "age": 30, "city": "New York"}

with open(file_name, "w") as file:
    json.dump(data, file)

print("Wrote JSON data to {}".format(file_name))

with open(file_name, "r") as file:
    loaded_data = json.load(file)

print("Read JSON data from {}:".format(file_name))
print(loaded_data)
