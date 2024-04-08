from animals.mammals import get_mammals, get_mammal_info

def main():
    mammals = get_mammals()
    print("Mammals:")
    for mammal in mammals:
        print(mammal)

    print("\nMammal info:")
    for mammal in mammals:
        print(get_mammal_info(mammal))

if __name__ == "__main__":
    main()
