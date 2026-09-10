#!/bin/bash

# Quick setup script for Mobile UI to PSD Converter (macOS/Linux)

echo ""
echo "========================================"
echo "Mobile UI to PSD Converter - Setup"
echo "========================================"
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python3 not found!"
    echo ""
    echo "Please install Python from: https://www.python.org/downloads/"
    echo "Or use: brew install python3 (macOS)"
    echo "Or use: sudo apt-get install python3 (Linux)"
    exit 1
fi

echo "✓ Python found: $(python3 --version)"
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment already exists"
fi

echo ""
echo "Activating virtual environment..."
source venv/bin/activate

echo ""
echo "Installing dependencies..."
echo "This may take 5-10 minutes (downloading models)..."
pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install dependencies"
    exit 1
fi

echo ""
echo "✓ Dependencies installed successfully!"
echo ""
echo "========================================"
echo "Setup Complete! Starting server..."
echo "========================================"
echo ""
echo "Server will start at: http://localhost:5000"
echo "Open this URL in your web browser"
echo ""
echo "Press Ctrl+C in terminal to stop the server"
echo ""

python3 app.py
