# pcd

Python decompiler using ChatGPT

## Setup

```bash
poetry install
poetry run pre-commit install
```

Set `OPENAI_API_KEY` environment variable. If you're using `direnv`, you can use `.envrc.template` as a template.
Put `src/pcd/logging.conf`. You can copy `src/pcd/logging.conf.template` like this:

```bash
cp src/pcd/logging.conf.template src/pcd/logging.conf
```

## Compile

```bash
poetry run pcd compile <directory | file> # you need to specify a directory or a file
```

## Decompile

```bash
poetry run pcd decompile [pyc-file] # you can specify a pyc file
```

## Examples

You can find examples in `examples` directory.

### Variables

original

```python
# Assigning values to variables
name = "John Doe"
age = 30
height = 6.1  # in feet

# Performing operations with variables
age_next_year = age + 1
half_height = height / 2

# Printing variables and their values
print("Name:", name)
print("Age:", age)
print("Height:", height, "feet")
print("Age next year:", age_next_year)
print("Half height:", half_height, "feet")

```

decompiled

```python
name = 'John Doe'
age = 30
height = 6.1
age_next_year = age + 1
half_height = height / 2

print('Name:', name)
print('Age:', age)
print('Height:', height, 'feet')
print('Age next year:', age_next_year)
print('Half height:', half_height, 'feet')
```

### Data types

original

```python
# Integer
integer_example = 42

# Float
float_example = 3.14

# String
string_example = "Hello, World!"

# List
list_example = [1, 2, 3, 4, 5]

# Tuple
tuple_example = (1, "apple", 3.14)

# Dictionary
dict_example = {
    "name": "John Doe",
    "age": 30,
    "city": "New York"
}

# Set
set_example = {1, 2, 3, 4, 5}

# Boolean
bool_example = True

# Printing the examples
print("Integer:", integer_example)
print("Float:", float_example)
print("String:", string_example)
print("List:", list_example)
print("Tuple:", tuple_example)
print("Dictionary:", dict_example)
print("Set:", set_example)
print("Boolean:", bool_example)

```

decompiled

```python
integer_example = 42
float_example = 3.14
string_example = 'Hello, World!'
list_example = [1, 2, 3, 4, 5]
tuple_example = (1, 'apple', 3.14)
dict_example = {'name': 'John Doe', 'age': 30, 'city': 'New York'}
set_example = {1, 2, 3, 4, 5}.union(frozenset({1, 2, 3, 4, 5}))
bool_example = True

print('Integer:', integer_example)
print('Float:', float_example)
print('String:', string_example)
print('List:', list_example)
print('Tuple:', tuple_example)
print('Dictionary:', dict_example)
print('Set:', set_example)
print('Boolean:', bool_example)
```

### Conditional statements (if-else)

original

```python
age = 25
country = "USA"
job_status = "employed"
favorite_color = "blue"

if age < 18 or (country == "USA" and favorite_color == "blue"):
    if job_status == "employed":
        print("Minor or USA + blue, employed.")
    elif job_status == "unemployed":
        print("Minor or USA + blue, unemployed.")
    else:
        print("Minor or USA + blue, other status.")
else:
    if job_status == "employed":
        if country != "USA" or favorite_color != "blue":
            print("Not minor, not USA + blue, employed.")
    elif job_status == "unemployed":
        if (country != "USA") ^ (favorite_color != "blue"):
            print("Not minor, not USA + blue, unemployed.")
    else:
        print("Not minor, not USA + blue, other status.")

```

decompiled

```python
age = 25
country = 'USA'
job_status = 'employed'
favorite_color = 'blue'

if age < 18 or (country == 'USA' and favorite_color == 'blue'):
    if job_status == 'employed':
        print('Minor or USA + blue, employed.')
    else:
        print('Minor or USA + blue, unemployed.')
else:
    if job_status == 'employed':
        if country != 'USA' or favorite_color != 'blue':
            print('Not minor, not USA + blue, employed.')
    else:
        if country != 'USA' or favorite_color != 'blue':
            print('Not minor, not USA + blue, unemployed.')
        else:
            print('Not minor, not USA + blue, other status.')
```

### Loops

original

```python
# Using a for loop to iterate through a list
fruits = ["apple", "banana", "orange", "grape"]
for fruit in fruits:
    print(f"Current fruit: {fruit}")

# Using a for loop with the range function
for i in range(5):
    print(f"Current value of i: {i}")

# Using a while loop
count = 0
while count < 5:
    print(f"Current count: {count}")
    count += 1

# Using a nested loop
for i in range(3):
    print(f"Outer loop, i: {i}")
    for j in range(2):
        print(f"  Inner loop, j: {j}")

```

decompiled

```python
fruits = ('apple', 'banana', 'orange', 'grape')
for fruit in fruits:
    print('Current fruit: {}'.format(fruit))
for i in range(5):
    print('Current value of i: {}'.format(i))
count = 0
while count < 5:
    print('Current count: {}'.format(count))
    count += 1
for i in range(3):
    print('Outer loop, i: {}'.format(i))
    for j in range(2):
        print('  Inner loop, j: {}'.format(j))
```

### List comprehensions

original

```python
# Using a for loop to iterate through a list
fruits = ["apple", "banana", "orange", "grape"]
for fruit in fruits:
    print(f"Current fruit: {fruit}")

# Using a for loop with the range function
for i in range(5):
    print(f"Current value of i: {i}")

# Using a while loop
count = 0
while count < 5:
    print(f"Current count: {count}")
    count += 1

# Using a nested loop
for i in range(3):
    print(f"Outer loop, i: {i}")
    for j in range(2):
        print(f"  Inner loop, j: {j}")

```

decompiled

```python
squares = [x**2 for x in range(1, 6)]
print(squares)

even_squares = [x**2 for x in range(1, 6) if x**2 % 2 == 0]
print(even_squares)

matrix = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
transpose = [[row[i] for row in matrix] for i in range(len(matrix))]
print(transpose)
```

### Functions

original

```python
global_var = "I'm a global variable"

def outer_function():
    outer_local_var = "I'm a local variable in the outer function"

    def inner_function():
        nonlocal outer_local_var
        inner_local_var = "I'm a local variable in the inner function"
        print(f"Inner function: {inner_local_var}")
        print(f"Inner function: {outer_local_var}")
        print(f"Inner function: {global_var}")

    print(f"Outer function: {outer_local_var}")
    print(f"Outer function: {global_var}")

    inner_function()

def calculate(operation, a, b):
    if operation == "add":
        return a + b
    elif operation == "subtract":
        return a - b
    elif operation == "multiply":
        return a * b
    elif operation == "divide":
        return a / b
    else:
        return None

# Test the outer_function
outer_function()

# Test the calculate function
print(calculate("add", 4, 5))

# Lambda function example
multiply = lambda x, y: x * y
print(multiply(3, 4))

```

decompiled

```python
global_var = "I'm a global variable"

def outer_function():
    outer_local_var = "I'm a local variable in the outer function"

    def inner_function():
        inner_local_var = "I'm a local variable in the inner function"
        print("Inner function: ", inner_local_var)
        print("Inner function: ", outer_local_var)
        print("Inner function: ", global_var)

    print("Outer function: ", outer_local_var)
    print("Outer function: ", global_var)
    inner_function()

def calculate(operation, a, b):
    if operation == 'add':
        return a + b
    elif operation == 'subtract':
        return a - b
    elif operation == 'multiply':
        return a * b
    elif operation == 'divide':
        return a / b
    else:
        return None

multiply = lambda x, y: x * y

print(calculate('add', 4, 5))
print(multiply(3, 4))
```

### Classes and objects

original

```python
class Animal:
    def __init__(self, name, age):
        self.name = name
        self.age = age

    def speak(self):
        print(f"{self.name} makes a generic animal sound.")

    def describe(self):
        print(f"{self.name} is {self.age} years old.")


class Dog(Animal):
    def __init__(self, name, age, breed):
        super().__init__(name, age)
        self.breed = breed

    def speak(self):
        print(f"{self.name} barks!")

    def describe_breed(self):
        print(f"{self.name} is a {self.breed}.")


class Cat(Animal):
    def __init__(self, name, age, color):
        super().__init__(name, age)
        self.color = color

    def speak(self):
        print(f"{self.name} meows!")

    def describe_color(self):
        print(f"{self.name} has a {self.color} coat.")


# Creating objects
animal = Animal("Generic animal", 3)
dog = Dog("Buddy", 5, "Golden Retriever")
cat = Cat("Whiskers", 7, "black")

# Calling methods on objects
animal.speak()
animal.describe()

dog.speak()
dog.describe()
dog.describe_breed()

cat.speak()
cat.describe()
cat.describe_color()

```

decompiled

```python
class Animal:
    def __init__(self, name, age):
        self.name = name
        self.age = age

    def speak(self):
        print(f"{self.name} makes a generic animal sound.")

    def describe(self):
        print(f"{self.name} is {self.age} years old.")


class Dog(Animal):
    def __init__(self, name, age, breed):
        super().__init__(name, age)
        self.breed = breed

    def speak(self):
        print(f"{self.name} barks!")

    def describe_breed(self):
        print(f"{self.name} is a {self.breed}.")


class Cat(Animal):
    def __init__(self, name, age, color):
        super().__init__(name, age)
        self.color = color

    def speak(self):
        print(f"{self.name} meows!")

    def describe_color(self):
        print(f"{self.name} has a {self.color} coat.")


animal = Animal("Generic animal", 3)
dog = Dog("Buddy", 5, "Golden Retriever")
cat = Cat("Whiskers", 7, "black")

animal.speak()
animal.describe()

dog.speak()
dog.describe()
dog.describe_breed()

cat.speak()
cat.describe()
cat.describe_color()
```

### Modules and packages

original

```python
from animals.mammals import get_mammals, get_mammal_info

def main():
    mammals = get_mammals()
    print("Mammals:")
    for mammal in mammals:
        print(mammal)

    print("\nMammal info:")
    for mammal in mammals:
        print(get_mammal_info(mammal))

if __name__ == "__main__":
    main()

```

decompiled

```python
from animals.mammals import get_mammals, get_mammal_info

def main():
    mammals = get_mammals()
    print('Mammals:')
    for mammal in mammals:
        print(mammal)
    print('\nMammal info:')
    for mammal in mammals:
        print(get_mammal_info(mammal))

if __name__ == '__main__':
    main()
```

### Exception handling

original

```python
def divide(a, b):
    try:
        result = a / b
        print(f"{a} divided by {b} is {result}")
    except ZeroDivisionError:
        print("Cannot divide by zero.")


divide(10, 2)
divide(10, 0)


def safe_conversion(value, to_int=True):
    try:
        if to_int:
            converted = int(value)
        else:
            converted = float(value)
        print(f"Converted {value} to {converted}")
    except ValueError:
        print(f"Invalid value: {value}")
    except TypeError:
        print(f"Unsupported type: {type(value).__name__}")


safe_conversion("42")
safe_conversion("3.14", False)
safe_conversion("abc")
safe_conversion(None)


def read_file(file_name):
    try:
        file = open(file_name, "r")
        content = file.read()
        print(f"File content:\n{content}")
    except FileNotFoundError:
        print("File not found.")
    finally:
        if "file" in locals() and not file.closed:
            file.close()
            print("File closed.")


read_file("example.txt")


class InvalidAgeError(ValueError):
    pass


def check_age(age):
    if age < 0:
        raise InvalidAgeError("Age cannot be negative.")
    elif age > 120:
        raise InvalidAgeError("Age is too high.")
    else:
        print("Age is valid.")


try:
    check_age(25)
    check_age(-5)
except InvalidAgeError as e:
    print(e)

```

decompiled

```python
def divide(a, b):
    try:
        result = a / b
    except ZeroDivisionError:
        print('Cannot divide by zero.')
    else:
        print(f'{a} divided by {b} is {result}')

def safe_conversion(value, to_int=True):
    try:
        if to_int:
            converted = int(value)
        else:
            converted = float(value)
    except ValueError:
        print(f'Invalid value: {value}')
    except TypeError:
        print(f'Unsupported type: {type(value).__name__}')
    else:
        print(f'Converted {value} to {converted}')

def read_file(file_name):
    try:
        file = open(file_name, 'r')
    except FileNotFoundError:
        print('File not found.')
    else:
        content = file.read()
        print(f'File content:\n{content}')
        try:
            locals()['file']
        except KeyError:
            pass
        else:
            if not file.closed:
                file.close()
                print('File closed.')
            raise

class InvalidAgeError(ValueError):
    pass

def check_age(age):
    if age < 0:
        raise InvalidAgeError('Age cannot be negative.')
    elif age > 120:
        raise InvalidAgeError('Age is too high.')
    else:
        print('Age is valid.')
```

### File I/O

original

```python
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

```

decompiled

```python
file_name = 'example.txt'

with open(file_name, 'r') as file:
    content = file.read()

print('File content:\n{}'.format(content))

try:
    with open('output.txt', 'w') as file:
        content = 'Hello, World!'
        file.write(content)

    print('Wrote content to {}'.format(file_name))
except:
    pass

try:
    with open('log.txt', 'a') as file:
        log_entry = 'This is a log entry.'
        file.write('{}\n'.format(log_entry))

    print('Appended log entry to {}'.format(file_name))
except:
    pass

file_name = 'example.txt'

print('Reading {} line by line:'.format(file_name))
with open(file_name, 'r') as file:
    for line in file:
        print(line.strip())

import json

file_name = 'data.json'
data = {'name': 'John', 'age': 30, 'city': 'New York'}

with open(file_name, 'w') as file:
    json.dump(data, file)

print('Wrote JSON data to {}'.format(file_name))

with open(file_name, 'r') as file:
    loaded_data = json.load(file)

print('Read JSON data from {}:'.format(file_name))
print(loaded_data)
```

### Standard library

original

```python
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

```

decompiled

```python
import os
import shutil
import glob
import tempfile

cwd = os.getcwd()
print('Current working directory: {0}'.format(cwd))

print('Files and directories in the current directory:')
for item in os.listdir(cwd):
    print(item)

new_dir = 'example_directory'
os.makedirs(new_dir, exist_ok=True)
print('Created new directory: {0}'.format(new_dir))

new_name = 'renamed_directory'
os.rename(new_dir, new_name)
print('Renamed directory from {0} to {1}'.format(new_dir, new_name))

os.rmdir(new_name)
print('Removed directory: {0}'.format(new_name))

src_file = 'source.txt'
dst_file = 'destination.txt'
shutil.copy(src_file, dst_file)
print('Copied {0} to {1}'.format(src_file, dst_file))

new_location = 'moved.txt'
shutil.move(dst_file, new_location)
print('Moved {0} to {1}'.format(dst_file, new_location))

os.remove(new_location)
print('Removed file: {0}'.format(new_location))

print('Python files in the current directory:')
for py_file in glob.glob('*.py'):
    print(py_file)

with tempfile.NamedTemporaryFile('w+t', delete=False) as temp_file:
    temp_file.write('Hello, World!')
    temp_path = temp_file.name
    print('Created temporary file: {0}'.format(temp_path))

with open(temp_path, 'r') as temp_file:
    content = temp_file.read()
    print('Content of the temporary file: {0}'.format(content))

os.remove(temp_path)
print('Removed temporary file: {0}'.format(temp_path))
```
