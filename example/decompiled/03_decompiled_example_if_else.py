age = 25
country = 'USA'
job_status = 'employed'
favorite_color = 'blue'

if age < 18 or (country == 'USA' and favorite_color == 'blue'):
    if job_status == 'employed':
        print('Minor or USA + blue, employed.')
    else:
        print('Minor or USA + blue, unemployed.')
else:
    if job_status == 'employed':
        if country != 'USA' or favorite_color != 'blue':
            print('Not minor, not USA + blue, employed.')
    else:
        if country != 'USA' or favorite_color != 'blue':
            print('Not minor, not USA + blue, unemployed.')
        else:
            print('Not minor, not USA + blue, other status.')