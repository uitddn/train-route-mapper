**Train Route Mapper**

Train Route Mapper is a Flask-based web application that allows users to visualize train routes on an interactive map. By entering a station code, users can see the routes of trains passing through that station, along with additional details such as train names, starting and terminating stations.


[![image.png](https://i.postimg.cc/636GJdrv/image.png)](https://postimg.cc/XZhv9BDj)


## Features
The main and most useful feature of this project the map visualisation of all train routes that pass through
your station. In a second you get the infomation that what extenet of india in length and breadth you can cover from
your nearest local station.

- **Interactive Map**: Displays train routes using Folium with MarkerCluster for better visualization.
- **Station Search**: Enter a station code (e.g., NDLS, GAYA, BZA) to fetch train routes.
- **Train Details**: View train numbers, names, and their starting/terminating stations in a scrollable table.
- **Feedback Submission**: Users can submit feedback directly through the website.
- **Caching**: Optimized with caching to improve performance for repeated queries.
- **Concurrency**: Uses multithreading to fetch train data efficiently.

## Tech Stack

- **Backend**: Flask
- **Frontend**: HTML, CSS, Jinja2 templates
- **Mapping**: Folium (Leaflet.js)
- **Data Handling**: Pandas, BeautifulSoup
- **Deployment**: Gunicorn

## Public Access

The website is publicly accessible at:  
**[http://13.61.27.152:5000/](http://13.61.27.152:5000/)**

## How It Works

1. **Station Code Input**: Users enter a station code in the input field.
2. **Train Route Fetching**: The app fetches train data from `etrain.info` and processes it.
3. **Map Generation**: Routes are plotted on an interactive map using Folium.
4. **Train Details Table**: A table displays train details for the selected station.
5. **Feedback**: Users can submit feedback, which is saved on the server.

## Project Structure

```
train-route-mapper/
├── app.py                # Main Flask application
├── templates/
│   └── index.html        # HTML template for the web interface
├── requirements.txt      # Python dependencies
├── Procfile              # Deployment configuration for Gunicorn
├── stations.xlsx         # Station coordinates data
└── feedback.txt          # Stores user feedback
```

## Installation (For Local Development)

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/train-route-mapper.git
   cd train-route-mapper
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   python app.py
   ```

4. Access the app locally at `http://127.0.0.1:5000/`.

## Deployment

The app is configured to run with Gunicorn. The Procfile specifies the command:
```plaintext
web: gunicorn --timeout 120 app:app
```

## Feedback

We welcome your feedback! Use the feedback form on the website to share your thoughts.

---

Enjoy exploring train routes with Train Route Mapper! 🚆
