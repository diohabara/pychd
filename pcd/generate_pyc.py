import argparse
import compileall
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Compile Python source files into bytecode.',
        epilog='Example: python generate_bytecode.py')
    parser.add_argument("directory", help="Directory to compile", type=str)
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    dir = Path(args.directory)
    compileall.compile_dir(dir)


if __name__ == '__main__':
    main()
