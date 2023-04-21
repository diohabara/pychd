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
