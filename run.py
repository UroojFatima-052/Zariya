import sys
import os

# Add backend/ to path so Python can find app.py, models.py, config.py, extensions.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from dotenv import load_dotenv
load_dotenv()

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
