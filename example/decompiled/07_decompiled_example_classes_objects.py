class Animal:
    def __init__(self, name, age):
        self.name = name
        self.age = age

    def speak(self):
        print(f"{self.name} makes a generic animal sound.")

    def describe(self):
        print(f"{self.name} is {self.age} years old.")


class Dog(Animal):
    def __init__(self, name, age, breed):
        super().__init__(name, age)
        self.breed = breed

    def speak(self):
        print(f"{self.name} barks!")

    def describe_breed(self):
        print(f"{self.name} is a {self.breed}.")


class Cat(Animal):
    def __init__(self, name, age, color):
        super().__init__(name, age)
        self.color = color

    def speak(self):
        print(f"{self.name} meows!")

    def describe_color(self):
        print(f"{self.name} has a {self.color} coat.")


animal = Animal("Generic animal", 3)
dog = Dog("Buddy", 5, "Golden Retriever")
cat = Cat("Whiskers", 7, "black")

animal.speak()
animal.describe()

dog.speak()
dog.describe()
dog.describe_breed()

cat.speak()
cat.describe()
cat.describe_color()