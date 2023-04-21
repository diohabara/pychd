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