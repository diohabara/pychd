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
