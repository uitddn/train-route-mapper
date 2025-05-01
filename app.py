import requests
from bs4 import BeautifulSoup
import folium
import pandas as pd
import random
import time
import sys
import concurrent.futures
import os  # Import os for robust file path handling

from flask import Flask, render_template, request, flash  # Import Flask components

# --- Flask App Initialization ---
app = Flask(__name__)
# A secret key is needed for flashing messages (optional but good practice)
app.secret_key = os.urandom(24)  # Generates a random secret key

# --- Configuration ---
# Use os.path.join for robust path handling, works locally and on servers
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE_PATH = os.path.join(BASE_DIR, 'stations.xlsx')
ETRAIN_INFO_BASE_URL_STATION = "https://etrain.info/station/{}/all"
ETRAIN_INFO_BASE_URL_TRAIN = "https://etrain.info/train/{}/schedule"
MAX_WORKERS = 2
REQUEST_TIMEOUT = 20
# Add User-Agent header
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}


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

    print(f"Loading station coordinates from: {file_path}")  # Log loading
    try:
        station_data = pd.read_excel(file_path)
        # Basic data cleaning and validation
        station_data.columns = station_data.columns.str.strip().str.upper()
        required_cols = ['STN CODE', 'LAT', 'LON']
        if not all(col in station_data.columns for col in required_cols):
            missing = [
                col for col in required_cols if col not in station_data.columns]
            print(f"Error: Missing required columns in {file_path}: {missing}")
            return None, None  # Return None to indicate failure

        station_data['LAT'] = pd.to_numeric(
            station_data['LAT'], errors='coerce')
        station_data['LON'] = pd.to_numeric(
            station_data['LON'], errors='coerce')
        station_data.dropna(subset=['LAT', 'LON'], inplace=True)

        coordinates = dict(zip(station_data['STN CODE'], zip(
            station_data['LAT'], station_data['LON'])))
        print(
            f"Successfully loaded coordinates for {len(coordinates)} stations.")

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
        return None  # Indicate error

    soup = BeautifulSoup(response.content, 'html.parser')
    train_numbers = []
    rows = soup.find_all("tr", {"data-train": True})

    if not rows:
        print(f"No train data found on page for station {station_code}.")
        return []  # No trains found is not an error, just empty list

    for row in rows:
        train_data_str = row.get("data-train")
        if train_data_str:
            try:
                # SECURITY WARNING: eval is risky. Use with caution on trusted data.
                train_info = eval(train_data_str)
                if isinstance(train_info, dict) and "num" in train_info:
                    train_numbers.append(str(train_info["num"]))
            except Exception as e:
                print(
                    f"Warning: Could not evaluate data-train attribute: {train_data_str}. Error: {e}")

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
        print(
            f"Error fetching schedule for train {formatted_train_number}: {e}")
        return train_number, None
    except Exception as e:
        print(
            f"Unexpected error processing train {formatted_train_number}: {e}")
        return train_number, None

    try:
        # Parsing logic (same as before)
        soup = BeautifulSoup(response.content, 'html.parser')
        schedule_table = soup.find(
            'table', class_=lambda x: x and 'schtbl' in x.split())
        station_codes = []
        if schedule_table:
            rows = schedule_table.find_all('tr')
            for row in rows:
                station_cell = row.find(
                    'td', class_=lambda x: x and 'stnc' in x.split())
                if station_cell:
                    link = station_cell.find(
                        'a', href=lambda x: x and '/station/' in x)
                    if link:
                        link_text = link.get_text(strip=True)
                        code_part = link_text.split('-')[0].strip()
                        if code_part:
                            station_codes.append(code_part)

        if not station_codes:  # Fallback
            source_select = soup.find('select', {'name': 'src'})
            if source_select:
                options = source_select.find_all('option')
                if options:
                    station_codes = [opt['value'] for opt in options if opt.has_attr(
                        'value') and opt['value']]

        return train_number, station_codes if station_codes else None

    except Exception as e:
        print(f"Error parsing HTML for train {formatted_train_number}: {e}")
        return train_number, None


def generate_map(station_code, station_coordinates, station_data_df, train_station_routes):
    """Generates the Folium map object."""
    print(f"Generating map for {len(train_station_routes)} routes...")
    if not station_coordinates or station_data_df is None or station_data_df.empty:
        # Use a default center if coordinate data is missing
        map_center = [20.5937, 78.9629]  # India center
        zoom_level = 5
    else:
        # Try to center on the specific station if its coords are known
        target_coords = station_coordinates.get(station_code)
        if target_coords:
            map_center = target_coords
            zoom_level = 8  # Zoom in a bit more if centered on the station
        else:
            # Fallback to average if target station coords missing
            map_center = [station_data_df['LAT'].mean(),
                          station_data_df['LON'].mean()]
            zoom_level = 6

    train_map = folium.Map(location=map_center,
                           zoom_start=zoom_level, tiles='CartoDB positron')

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
                        icon=folium.Icon(color='darkblue',
                                         icon='train', prefix='fa')
                    ).add_to(train_map)
                    added_station_markers.add(stn_code)
            else:
                # Log missing coordinates for debugging if needed
                # print(f"MapGen Warn: Coords missing for {stn_code} in train {train_number}")
                if len(route_points) > 1:
                    folium.PolyLine(route_points, color=color, weight=2, opacity=0.7,
                                    tooltip=f"Train: {train_number}").add_to(train_map)
                route_points = []  # Reset segment

        if len(route_points) > 1:
            folium.PolyLine(route_points, color=color, weight=2, opacity=0.7,
                            tooltip=f"Train: {train_number}").add_to(train_map)

    return train_map


# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
# --- Inside the index() function ---

@app.route('/', methods=['GET', 'POST'])
def index():
    map_html = None
    status_message = None
    error_message = None
    searched_station = None

    # ... (loading coordinates remains the same) ...

    if request.method == 'POST':
        # ... (getting station_code remains the same) ...

        # 1. Fetch Train Numbers
        train_numbers = get_trains_for_station(station_code)

        # ... (error handling for train_numbers fetch) ...

        total_trains_found = len(train_numbers)
        # --- Apply Plot Limit (Keep this if still needed for memory) ---
        PLOT_LIMIT = 75  # Keep or adjust this limit
        trains_to_process_list = train_numbers
        limit_applied = False
        if total_trains_found > PLOT_LIMIT:
            print(f"Limiting processing to {PLOT_LIMIT} out of {total_trains_found} trains found.")
            trains_to_process_list = train_numbers[:PLOT_LIMIT]
            limit_applied = True
        # --- Limit End ---

        total_trains_to_process = len(trains_to_process_list)

        # Adjust status message
        # ... (status message logic slightly adjusted) ...
        base_status = f"Found {total_trains_found} trains for {station_code}."
        if limit_applied:
            base_status += f" Processing first {total_trains_to_process} (limit applied)."
        status_message = base_status + f" Fetching routes (max {MAX_WORKERS} parallel)..."
        print(f"Processing {total_trains_to_process} trains (out of {total_trains_found} found)...")


        # 2. Fetch Routes Concurrently (Use trains_to_process_list)
        all_fetched_routes = {} # Store ALL fetched routes temporarily
        futures_map = {}
        processed_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for train_num in trains_to_process_list: # Use the limited list
                future = executor.submit(get_station_codes_for_train, train_num)
                futures_map[future] = train_num

            for future in concurrent.futures.as_completed(futures_map):
                original_train_num = futures_map[future]
                processed_count += 1
                try:
                    returned_train_num, station_codes = future.result()
                    if station_codes and len(station_codes) >= 2: # Need at least 2 stations for start/end
                        all_fetched_routes[returned_train_num] = station_codes
                    elif station_codes:
                         print(f"Warning: Route for train {returned_train_num} has less than 2 stations, cannot check for pairs.")
                         # Decide whether to include these short routes later or not
                         # all_fetched_routes[returned_train_num] = station_codes # Optional: keep them anyway?
                except Exception as exc:
                    print(f"Train {original_train_num} generated an exception during fetch/process: {exc}")

                if processed_count % 20 == 0 or processed_count == total_trains_to_process:
                    print(f"    Route fetching progress: {processed_count}/{total_trains_to_process}")


        # --- !!! 2.5 Filter Reverse Duplicates START !!! ---
        print(f"Fetched {len(all_fetched_routes)} valid routes. Filtering reverse duplicates...")
        train_station_routes = {} # This will hold the FINAL routes to plot
        processed_pairs = set()   # Keep track of numbers already included or skipped as a pair

        # Sort trains numerically to process consistently (e.g., keep 15013, skip 15014)
        # Handle potential non-numeric train numbers gracefully
        def sort_key(item):
            try: return int(item[0])
            except ValueError: return float('inf') # Put non-numeric ones at the end

        sorted_fetched_items = sorted(all_fetched_routes.items(), key=sort_key)

        for train_num_a, route_a in sorted_fetched_items:
            if train_num_a in processed_pairs:
                continue # Skip if already processed as part of a pair

            start_a = route_a[0]
            end_a = route_a[-1]
            pair_found_and_skipped = False

            # Search for the reverse pair *within the already fetched routes*
            for train_num_b, route_b in all_fetched_routes.items():
                 # Don't compare to itself, check if B is already processed, and ensure B has start/end
                 if train_num_a == train_num_b or train_num_b in processed_pairs or len(route_b) < 2:
                     continue

                 start_b = route_b[0]
                 end_b = route_b[-1]

                 # Check if B is the reverse of A
                 if start_a == end_b and end_a == start_b:
                     # Found the pair! Mark the second train (train_num_b) to be skipped.
                     # We will add train_num_a later.
                     print(f"  Identified pair: Keeping {train_num_a}, will skip {train_num_b}")
                     processed_pairs.add(train_num_b)
                     pair_found_and_skipped = True
                     # Optimization: Assuming only one direct reverse pair exists per train
                     break

            # Add train_num_a to the final list (it wasn't skipped)
            # Also add it to processed_pairs so it doesn't get processed again
            train_station_routes[train_num_a] = route_a
            processed_pairs.add(train_num_a)

        # --- !!! 2.5 Filter Reverse Duplicates END !!! ---


        final_route_count = len(train_station_routes)
        print(f"Filtered down to {final_route_count} unique direction routes.")

        if not train_station_routes:
             # Adjust message if filtering removed everything (unlikely but possible)
             status_message = f"Found {total_trains_found} trains for {station_code}, fetched {len(all_fetched_routes)} routes, but no unique direction routes remained after filtering (or none could be fetched)."
             # Check if limit was applied too
             if limit_applied: status_message += f" (Processed {total_trains_to_process} due to limit)."
             return render_template('index.html', status_message=status_message, searched_station=searched_station)

        # Adjust status message based on final count
        status_message = f"Successfully fetched {len(all_fetched_routes)} routes for {station_code}"
        if limit_applied: status_message += f" (processed {total_trains_to_process} due to limit)"
        status_message += f". Plotting {final_route_count} unique direction routes. Generating map..."


        # 3. Generate Map (using the filtered train_station_routes)
        try:
            # Pass the FILTERED dictionary to the map generator
            folium_map = generate_map(
                station_code, station_coordinates, station_data_df, train_station_routes)
            map_html = folium_map._repr_html_()
            # Adjust final status message
            status_message = f"Map generated for {final_route_count} unique direction train routes passing through {station_code}."
            if limit_applied: status_message += f" (Processed {total_trains_to_process} out of {total_trains_found} found due to limit)."

            print("Map generation complete.")
        except Exception as e:
            # ... (error handling for map generation) ...

    # ... (final render_template call) ...


# --- Main execution ---
if __name__ == '__main__':
    # Use waitress or gunicorn in production, Flask's built-in server is for development
    # For local testing:
    print("Starting Flask development server...")
    # Load coordinates once at startup when running locally for efficiency
    load_station_coordinates(EXCEL_FILE_PATH)
    # debug=True is helpful for development, turn off for production
    app.run(debug=True)

# --- Main execution ---
if __name__ == '__main__':
    # Port is configured by Render's environment variable $PORT
    # Bind to 0.0.0.0 to allow external connections within the container
    # Remove debug=True for production
    # Default to 5000 if PORT not set (for local testing)
    port = int(os.environ.get('PORT', 5000))
    # Use 'waitress' if gunicorn isn't working as expected on Windows dev machines,
    # but gunicorn is standard for Linux deployment on Render.
    # For production on Render, the Procfile's gunicorn command takes precedence.
    # This block is now mostly for potential local testing setup.
    print(f"Attempting to start server on port {port}")
    # app.run(host='0.0.0.0', port=port) # Keep this line if you want to test locally like Render will run it
