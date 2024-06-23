fruits = ("apple", "banana", "orange", "grape")
for fruit in fruits:
    print("Current fruit: {}".format(fruit))
for i in range(5):
    print("Current value of i: {}".format(i))
count = 0
while count < 5:
    print("Current count: {}".format(count))
    count += 1
for i in range(3):
    print("Outer loop, i: {}".format(i))
    for j in range(2):
        print("  Inner loop, j: {}".format(j))
