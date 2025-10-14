from pathlib import Path
import logging
import os

from dotenv import load_dotenv

class RelativePathFormatter(logging.Formatter):
    """A formatter that uses a relative path for the log source."""
    def format(self, record):
        # Assuming utils.py is in the project root directory.
        project_root = os.path.dirname(os.path.abspath(__file__))
        record.relativepath = os.path.relpath(record.pathname, project_root)
        return super().format(record)


def config_logger(log_file_name:str, log_file_dir:Path):
    log_file_dir.mkdir(parents=True,exist_ok=True)
    log_file_path = log_file_dir / log_file_name
    print(f"Log file path: {log_file_path}")

    # Get the root logger
    root_logger = logging.getLogger()

    # Clear existing handlers to avoid duplicate logs
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Configure root logger
    root_logger.setLevel(logging.INFO)
    formatter = RelativePathFormatter("%(asctime)s %(levelname)s [%(name)s] [%(relativepath)s:%(funcName)s():%(lineno)d] %(message)s")

    # Add file handler
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

def load_dotenvs():
    load_dotenv()
    load_dotenv(".credentials")
    
def convert_windows_to_unix_path(path:str)->str:
    return path.replace("\\","/")


if __name__ == "__main__":
    print("before",os.getenv("KODIUSER"))
    load_dotenvs()
    print("after",os.getenv("KODIUSER"))