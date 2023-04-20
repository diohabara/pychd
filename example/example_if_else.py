age = 25
location = "USA"
favorite_color = "blue"
is_employed = True

if age < 18 or (location == "USA" and favorite_color == "blue"):
    if is_employed and (location == "USA" or favorite_color == "red"):
        print("Minor or USA + blue, employed.")
    elif not is_employed and ((location == "USA") ^ (favorite_color == "red")):
        print("Minor or USA + blue, not employed.")
    else:
        print("Minor or USA + blue, other status.")
else:
    if is_employed and (location != "USA" and favorite_color != "blue"):
        print("Not minor, not USA + blue, employed.")
    elif not is_employed and ((location != "USA") ^ (favorite_color != "blue")):
        print("Not minor, not USA + blue, not employed.")
    else:
        print("Not minor, not USA + blue, other status.")
