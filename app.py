import requests
from bs4 import BeautifulSoup
import folium
from folium.plugins import MarkerCluster # Import MarkerCluster
import pandas as pd
import random
import time
import sys
import concurrent.futures
import os
from datetime import datetime
import re

from flask import Flask, render_template, request, flash, redirect, url_for, session

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE_PATH = os.path.join(BASE_DIR, 'stations.xlsx')
ETRAIN_INFO_BASE_URL_STATION = "https://etrain.info/station/{}/all"
ETRAIN_INFO_BASE_URL_TRAIN = "https://etrain.info/train/{}/schedule"
MAX_WORKERS = 25  # Keep lower for free tier memory constraints
REQUEST_TIMEOUT = 25 # Slightly increased timeout
PLOT_LIMIT = 300  # **** ADJUST THIS THRESHOLD AS NEEDED **** Max trains to process/plot
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# --- Caching ---
station_coordinates_cache = None
station_data_df_cache = None
cached_map = None
cached_train_data = None
cached_station_code = None

# --- Helper Functions ---

def load_station_coordinates(file_path):
    """Loads station coordinates, using cache if available."""
    global station_coordinates_cache, station_data_df_cache
    if station_coordinates_cache is not None and station_data_df_cache is not None:
        return station_coordinates_cache, station_data_df_cache

    print(f"Loading station coordinates from: {file_path}")
    try:
        station_data = pd.read_excel(file_path)
        station_data.columns = station_data.columns.str.strip().str.upper()
        required_cols = ['STN CODE', 'LAT', 'LON']
        if not all(col in station_data.columns for col in required_cols):
            missing = [col for col in required_cols if col not in station_data.columns]
            print(f"Error: Missing required columns in {file_path}: {missing}")
            return None, None

        station_data['LAT'] = pd.to_numeric(station_data['LAT'], errors='coerce')
        station_data['LON'] = pd.to_numeric(station_data['LON'], errors='coerce')
        station_data.dropna(subset=['LAT', 'LON'], inplace=True)

        coordinates = dict(zip(station_data['STN CODE'], zip(station_data['LAT'], station_data['LON'])))
        print(f"Successfully loaded coordinates for {len(coordinates)} stations.")

        station_coordinates_cache = coordinates
        station_data_df_cache = station_data
        return coordinates, station_data
    except FileNotFoundError:
        print(f"Error: The file '{file_path}' was not found.")
        return None, None
    except Exception as e:
        print(f"An unexpected error occurred while reading {file_path}: {e}")
        return None, None

def clear_cache():
    """Clears the cached map and train data."""
    global cached_map, cached_train_data, cached_station_code
    cached_map = None
    cached_train_data = None
    cached_station_code = None

def get_trains_for_station(station_code):
    """Fetches train numbers for a given station."""
    url = ETRAIN_INFO_BASE_URL_STATION.format(station_code.upper())
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching train list for station {station_code}: {e}")
        return None

    soup = BeautifulSoup(response.content, 'html.parser')
    train_numbers = []
    rows = soup.find_all("tr", {"data-train": True})

    if not rows:
        print(f"No train data found on page for station {station_code}.")
        return []

    for row in rows:
        train_data_str = row.get("data-train")
        if train_data_str:
            try:
                train_info = eval(train_data_str) # Use with caution
                if isinstance(train_info, dict) and "num" in train_info:
                    train_numbers.append(str(train_info["num"]))
            except Exception as e:
                print(f"Warning: Could not evaluate data-train attribute: {train_data_str}. Error: {e}")
    return train_numbers

def extract_train_info_from_soup(soup):
    """Extracts train number, name, starting and terminating stations from soup."""
    train_info = {}

    # Extract Train Number and Name from <title> or <h1>
    title_tag = soup.find('title')
    if title_tag:
        title_text = title_tag.text
        # Example: "Train Schedule of SARAIGHAT EXPRESS (12345) with Availability..."
        num_match = re.search(r'\((\d+)\)', title_text)
        name_match = re.search(r'Train Schedule of (.*?) \(', title_text)
        if num_match:
            train_info['number'] = num_match.group(1)
        if name_match:
            train_info['name'] = name_match.group(1).strip()
    if not train_info.get('name'):
        h1 = soup.find('h1')
        if h1:
            train_info['name'] = h1.get_text(strip=True)

    # Extract Starting and Terminating Station from span.mdtext or similar
    mdtext_span = soup.find('span', class_='mdtext')
    if mdtext_span:
        station_text = mdtext_span.text.strip()
        # Example: "HOWRAH JN to GUWAHATI"
        if 'to' in station_text:
            parts = station_text.split('to')
            train_info['start_name'] = parts[0].strip()
            train_info['end_name'] = parts[1].strip()

    return train_info

def get_station_codes_for_train(train_number):
    """Fetches station codes and train info for a single train."""
    try:
        formatted_train_number = f"{int(train_number):05d}"
    except ValueError:
        return train_number, None, None

    url = ETRAIN_INFO_BASE_URL_TRAIN.format(formatted_train_number)
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if response.status_code == 404:
            return train_number, None, None
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching schedule for train {formatted_train_number}: {e}")
        return train_number, None, None
    except Exception as e:
        print(f"Unexpected error processing train {formatted_train_number}: {e}")
        return train_number, None, None

    try:
        soup = BeautifulSoup(response.content, 'html.parser')
        train_info = extract_train_info_from_soup(soup)
        # Get schedule table
        schedule_table = soup.find('table', class_=lambda x: x and 'schtbl' in x.split())
        station_codes = []
        station_names = []
        if schedule_table:
            rows = schedule_table.find_all('tr')
            for row in rows:
                station_cell = row.find('td', class_=lambda x: x and 'stnc' in x.split())
                if station_cell:
                    link = station_cell.find('a', href=lambda x: x and '/station/' in x)
                    if link:
                        link_text = link.get_text(strip=True)
                        code_part = link_text.split('-')[0].strip()
                        name_part = '-'.join(link_text.split('-')[1:]).strip() if '-' in link_text else ''
                        if code_part:
                            station_codes.append(code_part)
                            station_names.append(name_part)
        elif soup.find('select', {'name': 'src'}):
            source_select = soup.find('select', {'name': 'src'})
            options = source_select.find_all('option')
            if options:
                station_codes = [opt['value'] for opt in options if opt.has_attr('value') and opt['value']]
                station_names = [opt.get_text(strip=True) for opt in options if opt.has_attr('value') and opt['value']]

        # Prepare info for table
        if station_codes:
            train_info['start_code'] = station_codes[0]
            train_info['end_code'] = station_codes[-1]
            if station_names:
                train_info['start_name'] = station_names[0]
                train_info['end_name'] = station_names[-1]
        else:
            train_info['start_code'] = train_info['end_code'] = ''
            if 'start_name' not in train_info:
                train_info['start_name'] = ''
            if 'end_name' not in train_info:
                train_info['end_name'] = ''

        return train_number, station_codes if station_codes else None, train_info
    except Exception as e:
        print(f"Error parsing HTML for train {formatted_train_number}: {e}")
        return train_number, None, None

def generate_map(station_code, station_coordinates, station_data_df, train_station_routes):
    """Generates the Folium map object using MarkerCluster."""
    print(f"Generating map for {len(train_station_routes)} unique direction routes...")
    if not station_coordinates or station_data_df is None or station_data_df.empty:
        map_center = [20.5937, 78.9629] # India center
        zoom_level = 5
    else:
        target_coords = station_coordinates.get(station_code)
        if target_coords:
            map_center = target_coords
            zoom_level = 8
        else:
            map_center = [station_data_df['LAT'].mean(), station_data_df['LON'].mean()]
            zoom_level = 6

    train_map = folium.Map(location=map_center, zoom_start=zoom_level, tiles='CartoDB positron')

    # Create a MarkerCluster layer
    marker_cluster = MarkerCluster(name="Train Stations").add_to(train_map)

    added_station_markers = set() # Still useful to avoid duplicate marker objects

    for train_number, stations in train_station_routes.items():
        route_points = []
        color = "#{:06x}".format(random.randint(0, 0xFFFFFF))

        for stn_code in stations:
            coords = station_coordinates.get(stn_code)
            if coords:
                route_points.append(coords)
                # Add marker only once to the cluster
                if stn_code not in added_station_markers:
                    folium.Marker(
                        location=coords,
                        popup=f"{stn_code}", tooltip=f"Station: {stn_code}",
                        icon=folium.Icon(color='darkblue', icon='info-sign') # Simpler icon
                    ).add_to(marker_cluster) # Add to cluster
                    added_station_markers.add(stn_code)
            else:
                # Draw partial polyline if coordinates were missing for a segment
                if len(route_points) > 1:
                    folium.PolyLine(route_points, color=color, weight=2, opacity=0.7,
                                    tooltip=f"Train: {train_number}").add_to(train_map)
                route_points = [] # Reset segment

        # Draw the complete or last segment of the polyline for this train
        if len(route_points) > 1:
            folium.PolyLine(route_points, color=color, weight=2, opacity=0.7,
                            tooltip=f"Train: {train_number}").add_to(train_map)

    # Optional: Add Layer Control if needed later
    # folium.LayerControl().add_to(train_map)

    return train_map

# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def index():
    global cached_map, cached_train_data, cached_station_code

    map_html = None
    status_message = None
    error_message = None
    searched_station = None
    train_table_data = []

    station_coordinates, station_data_df = load_station_coordinates(EXCEL_FILE_PATH)
    if station_coordinates is None:
        return render_template('index.html', error_message="FATAL ERROR: Could not load station coordinates file. Check server logs.")

    # --- If POST, use submitted station code ---
    if request.method == 'POST':
        station_code = request.form.get('station_code', '').strip().upper()
        searched_station = station_code
        session['last_station_code'] = station_code
    # --- If GET, but session has last station, use it ---
    elif session.get('last_station_code'):
        searched_station = session.get('last_station_code')

    # Extract station name from station_data_df
    station_name = None
    if station_data_df is not None and not station_data_df.empty:
        station_row = station_data_df[station_data_df['STN CODE'] == searched_station]
        if not station_row.empty:
            station_name = station_row.iloc[0, 1]  # Assuming the second column contains the station name

    # Check if the map and train data are already cached
    if searched_station == cached_station_code:
        print(f"Using cached data for station: {searched_station}")
        map_html = cached_map
        train_table_data = cached_train_data
        station_display = f"{searched_station}: {station_name}" if station_name else searched_station
        status_message = f"Showing cached map and data for {station_display}."
    else:
        # Clear the cache if a new station is searched
        clear_cache()

        if searched_station:
            print(f"Request received for station: {searched_station}")

            # 1. Fetch Train Numbers
            train_numbers = get_trains_for_station(searched_station)

            if train_numbers is None:
                error_message = f"Error fetching train list for {searched_station}. Website might be down or unreachable."
                return render_template('index.html', error_message=error_message, searched_station=searched_station)
            elif not train_numbers:
                station_display = f"{searched_station}: {station_name}" if station_name else searched_station
                status_message = f"No trains found passing through {station_display} according to etrain.info."
                return render_template('index.html', status_message=status_message, searched_station=searched_station)

            total_trains_found = len(train_numbers)

            # --- Apply Plot Limit ---
            trains_to_process_list = train_numbers
            limit_applied = False
            if total_trains_found > PLOT_LIMIT:
                print(f"Limiting processing to {PLOT_LIMIT} out of {total_trains_found} trains found.")
                trains_to_process_list = train_numbers[:PLOT_LIMIT]
                limit_applied = True
            total_trains_to_process = len(trains_to_process_list)

            # Build initial status message
            station_display = f"{searched_station}: {station_name}" if station_name else searched_station
            base_status = f"Found {total_trains_found} trains for {station_display}."
            if limit_applied:
                base_status += f" Processing first {total_trains_to_process} (limit {PLOT_LIMIT} applied)."
            else:
                base_status += f" Processing {total_trains_to_process} trains."
            status_message = base_status + f" Fetching routes (max {MAX_WORKERS} parallel)..."
            print(f"Processing {total_trains_to_process} trains (out of {total_trains_found} found)...")

            # 2. Fetch Routes Concurrently
            all_fetched_routes = {}
            all_train_infos = {}
            futures_map = {}
            processed_count = 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for train_num in trains_to_process_list:
                    future = executor.submit(get_station_codes_for_train, train_num)
                    futures_map[future] = train_num

                for future in concurrent.futures.as_completed(futures_map):
                    original_train_num = futures_map[future]
                    processed_count += 1
                    try:
                        returned_train_num, station_codes, train_info = future.result()
                        if station_codes and len(station_codes) >= 2:
                            all_fetched_routes[returned_train_num] = station_codes
                            if train_info:
                                all_train_infos[returned_train_num] = train_info
                    except Exception as exc:
                        print(f"Train {original_train_num} generated an exception during fetch/process: {exc}")

                    if processed_count % 20 == 0 or processed_count == total_trains_to_process:
                        print(f"    Route fetching progress: {processed_count}/{total_trains_to_process}")

            fetched_route_count = len(all_fetched_routes)
            print(f"Fetched {fetched_route_count} valid routes. Filtering reverse duplicates...")

            # 2.5 Filter Reverse Duplicates
            train_station_routes_final = {} # Holds final routes to plot
            processed_pairs = set()

            def sort_key(item): # Helper for sorting numerically
                try: return int(item[0])
                except ValueError: return float('inf')

            sorted_fetched_items = sorted(all_fetched_routes.items(), key=sort_key)

            for train_num_a, route_a in sorted_fetched_items:
                if train_num_a in processed_pairs: continue

                start_a = route_a[0]
                end_a = route_a[-1]
                pair_found_and_skipped = False

                for train_num_b, route_b in all_fetched_routes.items(): # Compare against original fetched list
                     if train_num_a == train_num_b or train_num_b in processed_pairs or len(route_b) < 2: continue
                     start_b = route_b[0]
                     end_b = route_b[-1]
                     if start_a == end_b and end_a == start_b:
                         print(f"  Identified pair: Keeping {train_num_a}, will skip {train_num_b}")
                         processed_pairs.add(train_num_b)
                         pair_found_and_skipped = True
                         break # Found the pair for train_a

                # Add train_a to the final list and mark as processed
                train_station_routes_final[train_num_a] = route_a
                processed_pairs.add(train_num_a)

            # Prepare table data for template
            train_table_data = []
            for train_num, route in train_station_routes_final.items():
                info = all_train_infos.get(train_num, {})
                train_table_data.append({
                    'number': info.get('number', train_num),
                    'name': info.get('name', ''),
                    'start_code': info.get('start_code', route[0] if route else ''),
                    'start_name': info.get('start_name', ''),
                    'end_code': info.get('end_code', route[-1] if route else ''),
                    'end_name': info.get('end_name', ''),
                })

            final_route_count = len(train_station_routes_final)
            print(f"Filtered down to {final_route_count} unique direction routes.")

            if not train_station_routes_final:
                 status_message = f"Found {total_trains_found} trains for {searched_station}"
                 if limit_applied: status_message += f" (processed {total_trains_to_process} due to limit)"
                 status_message += f", fetched {fetched_route_count} routes, but no unique direction routes remained after filtering."
                 return render_template('index.html', status_message=status_message, searched_station=searched_station)

            # Update status before map generation
            station_display = f"{searched_station}: {station_name}" if station_name else searched_station
            status_message = f"Successfully fetched {fetched_route_count} routes for {station_display}."
            if limit_applied:
                status_message += f" (processed {total_trains_to_process} due to limit)"
            status_message += f". Plotting {final_route_count} unique direction routes. Generating map..."

            # 3. Generate Map (using filtered routes)
            try:
                folium_map = generate_map(
                    searched_station, station_coordinates, station_data_df, train_station_routes_final)
                map_html = folium_map._repr_html_()

                # Final status message
                station_display = f"{searched_station}: {station_name}" if station_name else searched_station
                status_message = f"Map generated for {final_route_count} unique direction train routes passing through {station_display}."
                if limit_applied:
                    status_message += f" (Processed {total_trains_to_process} out of {total_trains_found} found due to limit)."
                print("Map generation complete.")

                # Cache the generated map and train data
                cached_map = map_html
                cached_train_data = train_table_data
                cached_station_code = searched_station

            except Exception as e:
                print(f"Error during map generation: {e}")
                error_message = "An error occurred while generating the map (potentially too much data)."
                # Return template with error, keeping previous status context
                return render_template('index.html', error_message=error_message, status_message=status_message, searched_station=searched_station)
        else:
            # No station searched yet, just render the page
            pass

    # --- Render page ---
    return render_template('index.html', map_html=map_html, status_message=status_message, error_message=error_message, searched_station=searched_station, train_table_data=train_table_data)

@app.route('/submit_feedback', methods=['POST'])
def submit_feedback():
    feedback = request.form.get('feedback', '').strip()
    if feedback:
        feedback_file = os.path.join(BASE_DIR, 'feedback.txt')
        with open(feedback_file, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {feedback}\n")
        flash("Thank you for your feedback!", "success")
    else:
        flash("Feedback cannot be empty.", "error")
    # Redirect to index, which will restore the last searched station and map
    return redirect(url_for('index'))

# --- Main execution (for Render/Gunicorn) ---
# The Procfile `gunicorn app:app` will run this.
# The block below is mainly for local development testing.
if __name__ == '__main__':
    print("Starting Flask development server for local testing...")
    load_station_coordinates(EXCEL_FILE_PATH) # Pre-load cache locally
    # Use debug=True ONLY for local development
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
