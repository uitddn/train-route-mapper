import requests
from bs4 import BeautifulSoup
import folium
import pandas as pd
import random
import time
import sys
import concurrent.futures
import os # Import os for robust file path handling

from flask import Flask, render_template, request, flash # Import Flask components

# --- Flask App Initialization ---
app = Flask(__name__)
# A secret key is needed for flashing messages (optional but good practice)
app.secret_key = os.urandom(24) # Generates a random secret key

# --- Configuration ---
# Use os.path.join for robust path handling, works locally and on servers
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE_PATH = os.path.join(BASE_DIR, 'stations.xlsx')
ETRAIN_INFO_BASE_URL_STATION = "https://etrain.info/station/{}/all"
ETRAIN_INFO_BASE_URL_TRAIN = "https://etrain.info/train/{}/schedule"
MAX_WORKERS = 10
REQUEST_TIMEOUT = 20
# Add User-Agent header
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}


# --- Helper Functions (Adapted from your previous script) ---

# Cache coordinates to avoid reading the file on every request (optional but recommended)
station_coordinates_cache = None
station_data_df_cache = None

def load_station_coordinates(file_path):
    """Loads station coordinates, using cache if available."""
    global station_coordinates_cache, station_data_df_cache
    if station_coordinates_cache and station_data_df_cache is not None:
         # print("Using cached coordinates") # Optional debug print
         return station_coordinates_cache, station_data_df_cache

    print(f"Loading station coordinates from: {file_path}") # Log loading
    try:
        station_data = pd.read_excel(file_path)
        # Basic data cleaning and validation
        station_data.columns = station_data.columns.str.strip().str.upper()
        required_cols = ['STN CODE', 'LAT', 'LON']
        if not all(col in station_data.columns for col in required_cols):
            missing = [col for col in required_cols if col not in station_data.columns]
            print(f"Error: Missing required columns in {file_path}: {missing}")
            return None, None # Return None to indicate failure

        station_data['LAT'] = pd.to_numeric(station_data['LAT'], errors='coerce')
        station_data['LON'] = pd.to_numeric(station_data['LON'], errors='coerce')
        station_data.dropna(subset=['LAT', 'LON'], inplace=True)

        coordinates = dict(zip(station_data['STN CODE'], zip(station_data['LAT'], station_data['LON'])))
        print(f"Successfully loaded coordinates for {len(coordinates)} stations.")

        # Store in cache
        station_coordinates_cache = coordinates
        station_data_df_cache = station_data
        return coordinates, station_data

    except FileNotFoundError:
        print(f"Error: The file '{file_path}' was not found.")
        return None, None
    except Exception as e:
        print(f"An unexpected error occurred while reading {file_path}: {e}")
        return None, None


def get_trains_for_station(station_code):
    """Fetches train numbers for a given station."""
    # print(f"Fetching trains for station: {station_code}") # Less verbose logging for web app
    url = ETRAIN_INFO_BASE_URL_STATION.format(station_code.upper())
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching train list for station {station_code}: {e}")
        return None # Indicate error

    soup = BeautifulSoup(response.content, 'html.parser')
    train_numbers = []
    rows = soup.find_all("tr", {"data-train": True})

    if not rows:
        print(f"No train data found on page for station {station_code}.")
        return [] # No trains found is not an error, just empty list

    for row in rows:
        train_data_str = row.get("data-train")
        if train_data_str:
            try:
                # SECURITY WARNING: eval is risky. Use with caution on trusted data.
                train_info = eval(train_data_str)
                if isinstance(train_info, dict) and "num" in train_info:
                    train_numbers.append(str(train_info["num"]))
            except Exception as e:
                print(f"Warning: Could not evaluate data-train attribute: {train_data_str}. Error: {e}")

    # print(f"Found {len(train_numbers)} trains for station {station_code}.") # Less verbose
    return train_numbers


def get_station_codes_for_train(train_number):
    """Fetches station codes for a single train (designed for concurrency)."""
    try:
        formatted_train_number = f"{int(train_number):05d}"
    except ValueError:
        return train_number, None

    url = ETRAIN_INFO_BASE_URL_TRAIN.format(formatted_train_number)
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if response.status_code == 404:
             return train_number, None
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        # Log errors server-side, don't flood user interface
        print(f"Error fetching schedule for train {formatted_train_number}: {e}")
        return train_number, None
    except Exception as e:
         print(f"Unexpected error processing train {formatted_train_number}: {e}")
         return train_number, None

    try:
        # Parsing logic (same as before)
        soup = BeautifulSoup(response.content, 'html.parser')
        schedule_table = soup.find('table', class_=lambda x: x and 'schtbl' in x.split())
        station_codes = []
        if schedule_table:
            rows = schedule_table.find_all('tr')
            for row in rows:
                station_cell = row.find('td', class_=lambda x: x and 'stnc' in x.split())
                if station_cell:
                    link = station_cell.find('a', href=lambda x: x and '/station/' in x)
                    if link:
                        link_text = link.get_text(strip=True)
                        code_part = link_text.split('-')[0].strip()
                        if code_part: station_codes.append(code_part)

        if not station_codes: # Fallback
            source_select = soup.find('select', {'name': 'src'})
            if source_select:
                options = source_select.find_all('option')
                if options: station_codes = [opt['value'] for opt in options if opt.has_attr('value') and opt['value']]

        return train_number, station_codes if station_codes else None

    except Exception as e:
        print(f"Error parsing HTML for train {formatted_train_number}: {e}")
        return train_number, None


def generate_map(station_code, station_coordinates, station_data_df, train_station_routes):
    """Generates the Folium map object."""
    print(f"Generating map for {len(train_station_routes)} routes...")
    if not station_coordinates or station_data_df is None or station_data_df.empty:
        # Use a default center if coordinate data is missing
        map_center = [20.5937, 78.9629] # India center
        zoom_level = 5
    else:
        # Try to center on the specific station if its coords are known
        target_coords = station_coordinates.get(station_code)
        if target_coords:
            map_center = target_coords
            zoom_level = 8 # Zoom in a bit more if centered on the station
        else:
            # Fallback to average if target station coords missing
            map_center = [station_data_df['LAT'].mean(), station_data_df['LON'].mean()]
            zoom_level = 6

    train_map = folium.Map(location=map_center, zoom_start=zoom_level, tiles='CartoDB positron')

    added_station_markers = set()
    for train_number, stations in train_station_routes.items():
        route_points = []
        color = "#{:06x}".format(random.randint(0, 0xFFFFFF))

        for stn_code in stations:
            coords = station_coordinates.get(stn_code)
            if coords:
                route_points.append(coords)
                if stn_code not in added_station_markers:
                    folium.Marker(
                        location=coords,
                        popup=f"{stn_code}", tooltip=f"Station: {stn_code}",
                        icon=folium.Icon(color='darkblue', icon='train', prefix='fa')
                    ).add_to(train_map)
                    added_station_markers.add(stn_code)
            else:
                 # Log missing coordinates for debugging if needed
                 # print(f"MapGen Warn: Coords missing for {stn_code} in train {train_number}")
                 if len(route_points) > 1:
                     folium.PolyLine(route_points, color=color, weight=2, opacity=0.7, tooltip=f"Train: {train_number}").add_to(train_map)
                 route_points = [] # Reset segment

        if len(route_points) > 1:
             folium.PolyLine(route_points, color=color, weight=2, opacity=0.7, tooltip=f"Train: {train_number}").add_to(train_map)

    return train_map


# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def index():
    map_html = None
    status_message = None
    error_message = None
    searched_station = None

    # Load coordinates on first request or if cache is empty
    station_coordinates, station_data_df = load_station_coordinates(EXCEL_FILE_PATH)
    if station_coordinates is None:
        # Critical error if coordinates can't be loaded
        return render_template('index.html', error_message="FATAL ERROR: Could not load station coordinates file. Check server logs.")

    if request.method == 'POST':
        station_code = request.form.get('station_code', '').strip().upper()
        searched_station = station_code # Store for display

        if not station_code:
            error_message = "Please enter a station code."
            return render_template('index.html', error_message=error_message)

        status_message = f"Fetching data for station: {station_code}..."
        print(f"Request received for station: {station_code}") # Server log

        # 1. Fetch Train Numbers
        train_numbers = get_trains_for_station(station_code)

        if train_numbers is None:
            error_message = f"Error fetching train list for {station_code}. The website might be down or the station code invalid."
            return render_template('index.html', error_message=error_message, searched_station=searched_station)
        elif not train_numbers:
            status_message = f"No trains found passing through {station_code} according to etrain.info."
            return render_template('index.html', status_message=status_message, searched_station=searched_station)

        total_trains = len(train_numbers)
        status_message = f"Found {total_trains} trains for {station_code}. Fetching routes (max {MAX_WORKERS} parallel)... This may take a moment."
        print(f"Found {total_trains} trains. Fetching routes...")

        # 2. Fetch Routes Concurrently
        train_station_routes = {}
        futures_map = {}
        processed_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for train_num in train_numbers:
                future = executor.submit(get_station_codes_for_train, train_num)
                futures_map[future] = train_num

            for future in concurrent.futures.as_completed(futures_map):
                original_train_num = futures_map[future]
                processed_count += 1
                try:
                    returned_train_num, station_codes = future.result()
                    if station_codes:
                        train_station_routes[returned_train_num] = station_codes
                except Exception as exc:
                    print(f"Train {original_train_num} generated an exception during fetch/process: {exc}")
                # Optional: Update status periodically? Could be complex with web requests.
                # Simple print for server log:
                if processed_count % 20 == 0 or processed_count == total_trains:
                    print(f"    Route fetching progress: {processed_count}/{total_trains}")


        if not train_station_routes:
             status_message = f"Found {total_trains} trains for {station_code}, but could not fetch route details for any of them. Website structure might have changed or trains have no listed stops."
             return render_template('index.html', status_message=status_message, searched_station=searched_station)

        status_message = f"Successfully fetched routes for {len(train_station_routes)} out of {total_trains} trains for {station_code}. Generating map..."
        print(f"Fetched routes for {len(train_station_routes)} trains. Generating map.")

        # 3. Generate Map
        try:
            folium_map = generate_map(station_code, station_coordinates, station_data_df, train_station_routes)
            # Get the HTML representation of the map
            map_html = folium_map._repr_html_()
            status_message = f"Map generated for {len(train_station_routes)} train routes passing through {station_code}."
            print("Map generation complete.")
        except Exception as e:
            print(f"Error during map generation: {e}")
            error_message = "An error occurred while generating the map."
            # Still render the template, but show the error
            return render_template('index.html', error_message=error_message, status_message=status_message, searched_station=searched_station)


    # Render the template:
    # - On GET: Show empty form
    # - On POST: Show form (with previous input) and potentially status, error, or map
    return render_template('index.html', map_html=map_html, status_message=status_message, error_message=error_message, searched_station=searched_station)


# --- Main execution ---
if __name__ == '__main__':
    # Use waitress or gunicorn in production, Flask's built-in server is for development
    # For local testing:
    print("Starting Flask development server...")
    # Load coordinates once at startup when running locally for efficiency
    load_station_coordinates(EXCEL_FILE_PATH)
    app.run(debug=True) # debug=True is helpful for development, turn off for production
    
# --- Main execution ---
if __name__ == '__main__':
    # Port is configured by Render's environment variable $PORT
    # Bind to 0.0.0.0 to allow external connections within the container
    # Remove debug=True for production
    port = int(os.environ.get('PORT', 5000)) # Default to 5000 if PORT not set (for local testing)
    # Use 'waitress' if gunicorn isn't working as expected on Windows dev machines,
    # but gunicorn is standard for Linux deployment on Render.
    # For production on Render, the Procfile's gunicorn command takes precedence.
    # This block is now mostly for potential local testing setup.
    print(f"Attempting to start server on port {port}")
    # app.run(host='0.0.0.0', port=port) # Keep this line if you want to test locally like Render will run it