def divide(a, b):
    try:
        result = a / b
    except ZeroDivisionError:
        print("Cannot divide by zero.")
    else:
        print(f"{a} divided by {b} is {result}")


def safe_conversion(value, to_int=True):
    try:
        if to_int:
            converted = int(value)
        else:
            converted = float(value)
    except ValueError:
        print(f"Invalid value: {value}")
    except TypeError:
        print(f"Unsupported type: {type(value).__name__}")
    else:
        print(f"Converted {value} to {converted}")


def read_file(file_name):
    try:
        file = open(file_name, "r")
    except FileNotFoundError:
        print("File not found.")
    else:
        content = file.read()
        print(f"File content:\n{content}")
        try:
            locals()["file"]
        except KeyError:
            pass
        else:
            if not file.closed:
                file.close()
                print("File closed.")
            raise


class InvalidAgeError(ValueError):
    pass


def check_age(age):
    if age < 0:
        raise InvalidAgeError("Age cannot be negative.")
    elif age > 120:
        raise InvalidAgeError("Age is too high.")
    else:
        print("Age is valid.")
