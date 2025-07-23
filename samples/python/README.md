# Python DevContainer Sample

This sample demonstrates a Python development environment with DevContainer support.

## Features

- Python 3.12 with Jupyter Lab
- Flask web application
- Data science libraries (pandas, numpy, matplotlib)
- Development tools (black, pylint, pytest)
- VS Code extensions for Python development

## Getting Started

1. The devcontainer will automatically run `pip install -r requirements.txt`
2. Start the Flask app: `python app.py`
3. Access the app at http://localhost:5000
4. Run Jupyter Lab: `jupyter lab`

## Development

- Format code: `black *.py`
- Lint code: `pylint app.py`
- Run tests: `pytest`
