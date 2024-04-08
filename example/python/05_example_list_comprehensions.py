# Basic list comprehension
squares = [x**2 for x in range(1, 6)]
print(squares)  # Output: [1, 4, 9, 16, 25]

# List comprehension with a condition
even_squares = [x**2 for x in range(1, 6) if x % 2 == 0]
print(even_squares)  # Output: [4, 16]

# Nested list comprehension
matrix = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
transpose = [[row[i] for row in matrix] for i in range(len(matrix))]
print(transpose)  # Output: [[1, 4, 7], [2, 5, 8], [3, 6, 9]]
