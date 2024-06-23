squares = [x**2 for x in range(1, 6)]
print(squares)

even_squares = [x**2 for x in range(1, 6) if x**2 % 2 == 0]
print(even_squares)

matrix = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
transpose = [[row[i] for row in matrix] for i in range(len(matrix))]
print(transpose)
