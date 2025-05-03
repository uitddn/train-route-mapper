# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# --no-cache-dir makes the image smaller
# --trusted-host pypi.python.org avoids potential SSL issues in some environments
RUN pip install --no-cache-dir --trusted-host pypi.python.org -r requirements.txt

# Copy the rest of the application code into the container at /app
COPY . .

# Make port 5000 available to the world outside this container (Flask default port)
# Change this if your app runs on a different port
EXPOSE 5000

# Define environment variable (optional, but good practice for Flask)
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# Run app.py when the container launches
# Use "flask run" command to start the development server
CMD ["flask", "run"]