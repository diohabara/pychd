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
