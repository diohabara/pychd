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
