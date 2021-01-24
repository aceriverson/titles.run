from flask import Flask, request, abort, Response, jsonify, redirect
import requests
import time
import SECRETS
import dates
from flaskext.mysql import MySQL
import datetime
import random
import re
import flexpolyline as fp

app = Flask(__name__, static_url_path='')

mysql = MySQL()
app.config['MYSQL_DATABASE_USER'] = SECRETS.database_username
app.config['MYSQL_DATABASE_PASSWORD'] = SECRETS.database_password
app.config['MYSQL_DATABASE_DB'] = SECRETS.usernamedatabase
app.config['MYSQL_DATABASE_HOST'] = SECRETS.database_uri
mysql.init_app(app)

conn = mysql.connect()

@app.route('/')
def hello():
    return app.send_static_file('index.html')


@app.route('/authorize', methods=['GET'])
def authorize():
    try:
        request.args["scope"]

        files = {
            'client_id': (None, SECRETS.client_id),
            'client_secret': (None, SECRETS.client_secret),
            'code': (None, request.args['code']),
            'grant_type': (None, 'authorization_code'),
        }

        response = requests.post('https://www.strava.com/oauth/token', files=files)

        conn = mysql.connect()
        sql = 'INSERT INTO users (id, token, refresh_token, expires) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE token = %s, refresh_token = %s, expires = %s'
        val = (response.json()["athlete"]["id"], response.json()["access_token"], response.json()["refresh_token"], response.json()["expires_at"], response.json()["access_token"], response.json()["refresh_token"], response.json()["expires_at"])

        cursor = conn.cursor()
        cursor.execute(sql, val)
        cursor.close()
        conn.commit()
        conn.close()

        return redirect('/')
    except:
        return redirect("http://www.strava.com/oauth/authorize?client_id=%s&response_type=code&redirect_uri=%s/authorize&approval_prompt=force&scope=read,activity:read,activity:read_all,activity:write" % (SECRETS.client_id, SECRETS.url))


@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    if request.method == 'POST':
        activity = request.json
        try:
            # Determines if the webhook post is new, will also attempt to refresh the user token.
            if activity["aspect_type"] == "create" and refresh_token(activity["owner_id"]):
                run_title(activity)

            # If user #title or #totd
            if activity["aspect_type"] == "update" and refresh_token(activity["owner_id"]):
                if "#title" in activity["updates"]["title"]:
                    run_title(activity)

                if "totd" in activity["updates"]["title"]:
                    conn = mysql.connect()
                    cursor = conn.cursor()
                    sql = 'SELECT token FROM users WHERE id = %s'
                    val = (activity["owner_id"])
                    cursor.execute(sql, val)
                    # User token will be used to access activity data
                    user_token = cursor.fetchone()[0]
                    cursor.close()
                    conn.close()

                    activity_data = get_activity(user_token, activity["object_id"])

                    event = random_date_title(activity_data)
                    activity_type = get_type(activity_data)

                    set_title(event + " " + activity_type, user_token, activity_data)

        except KeyError:
            return '', 400

        return '', 200



    elif request.method == 'GET':
        response = request.args.to_dict()
        try:
            if response["hub.verify_token"] == SECRETS.verify_token:
                print('returning')
                return '{"hub.challenge": "%s"}' % response["hub.challenge"], 200
        except:
            return "You aren't supposed to be here"


def run_title(activity):
    conn = mysql.connect()
    cursor = conn.cursor()
    sql = 'SELECT token FROM users WHERE id = %s'
    val = (activity["owner_id"])
    cursor.execute(sql, val)
    # User token will be used to access activity data
    user_token = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    # All activity data in JSON form can be accessed through activity_data object
    activity_data = get_activity(user_token, activity["object_id"])

    # Stops the algorithm if the activity is manual/no gps
    if activity_data["start_latlng"] is None:
        return '', 200

    # These 3 variables checked for every title
    significant_elevation = get_elevation(activity_data)    # Either "Hilly " or ""
    starting_location = get_location(activity_data)         # [station identifier, city] or [None, ""]
    weather_conditions = get_weather(starting_location[0], activity_data["start_date"])    # String of weather (Rainy, etc..) or ""

    # If multiple top 3 segments are found, we will title it as segment hunt, end algorithm
    segments = get_crs(activity_data)
    if segments != "":
        set_title(significant_elevation + weather_conditions + segments + " Segment Hunt", user_token, activity_data)
        return '', 200

    activity_type = get_type(activity_data)                 # String of activity type (Run, Nordic Ski, etc...)
    poi = get_poi(user_token, activity_data)                # Either string of poi name or ""

    # If poi is found, clear city from weather data
    if poi:
        starting_location[1] = ""
        poi = " at " + poi

    # Finally sets title
    set_title(significant_elevation + weather_conditions + starting_location[1] + activity_type + poi, user_token, activity_data)


# Takes user token and activity id, returns detailed activity data
def get_activity(token, activity):
    print('getting activity, %s' % activity)
    url = "https://www.strava.com/api/v3/activities/%s" % activity
    headers = {'Authorization' : 'Bearer %s' % token}

    response = requests.get(url, headers=headers)

    return(response.json())


# Takes user token and activity id, returns coordinate stream
def get_coord_stream(token, activity):
    headers = {'Authorization' : 'Bearer %s' % token}
    url = "https://www.strava.com/api/v3/activities/%s/streams?keys=latlng&key_by_type=true" % activity

    r = requests.get(url, headers=headers)

    return r.json()


# Takes activity data, returns hyphenated list of segments where user achieved top 3 (eg. Akin-Pilot-Rd)
def get_crs(activity):
    course_records = []
    for segment in activity["segment_efforts"]:
        try:
            if segment["achievements"][0]["type"] == "overall" and segment["achievements"][0]["rank"] <= 3:
                course_records.append(segment["name"])
        except:
            continue

    course_records = " ".join(course_records)
    course_records = re.findall("([A-Z][a-z]+)", course_records)

    occurrences = []
    keyword_count = 0
    for word in course_records:
        count = sum(word in s for s in course_records)
        if keyword_count == count:
            if word not in occurrences:
                occurrences.append(word)
        elif keyword_count < count:
            occurrences = [word]
            keyword_count = count

    occurrences = "-".join(occurrences[0:3])

    return occurrences


# Returns true if activity is run with significant elevation, false if not run or not significant elevation
def get_elevation(activity):
    if activity["type"] == 'Run' and activity["total_elevation_gain"] / activity["distance"] >= 0.0125:
        return "Hilly "
    else:
        return ""


# Takes user token and activity data. Returns None or most relevant point of interest name. Will be reworked in v2
def get_poi(token, activity):
    try:
        coord_stream = get_coord_stream(token, activity["id"])
        polyline = fp.encode(coord_stream["latlng"]["data"])

        r = requests.get("https://browse.search.hereapi.com/v1/browse?apiKey=%s&at=%s,%s&route=%s;w=400&categories=350,550-5510-0359,800-8600-0193&limit=10" % (SECRETS.here_key, activity["start_latitude"], activity["start_longitude"], polyline))
        r = r.json()
    except:
        return ""
    relevant_location = {"name": "", "references": 0}
    try:
        for location in r["items"]:
            try:
                if len(location["references"]) > relevant_location["references"]:
                    relevant_location["name"] = location["title"]
                    relevant_location["references"] = len(location["references"])
            except:
                None
    except:
        return ""

    return relevant_location["name"]


# Takes location array, index 0 is a valid station id, plus time stamp. Returns weather condition (Rainy, Snowy, Windy) or None
def get_weather(location, time_of):
    if location is None:
        return None

    date_time_obj = datetime.datetime.strptime(time_of, '%Y-%m-%dT%H:%M:%SZ')
    date_time_obj += datetime.timedelta(hours=1)
    time_future = date_time_obj.strftime("%Y-%m-%dT%H:%M:%SZ")
    weather_info = requests.get("https://api.weather.gov/stations/%s/observations?start=%s&end=%s" % (location, time_of, time_future))
    weather_info = weather_info.json()

    try:
        conditions = re.findall("Rain|Snow|Wind", weather_info["features"][0]["properties"]["textDescription"])
        if conditions:
            return conditions[0] + "y "
        else:
            return ""

    except:
        return ""


# Takes activity data, returns array [(nearest station identifier), (city name)]
def get_location(activity):
    try:
        point_info = requests.get("https://api.weather.gov/points/%s,%s" % (activity["start_latlng"][0], activity["start_latlng"][1]))
        point_info = point_info.json()
        stations = requests.get(point_info["properties"]["observationStations"])
        stations = stations.json()
        nearest_station = stations["features"][0]["properties"]["stationIdentifier"]

        return [nearest_station, point_info["properties"]["relativeLocation"]["properties"]["city"] + " "]

    except:
        return [None, ""]

        # date_time_obj = datetime.datetime.strptime(activity["start_date"], '%Y-%m-%dT%H:%M:%SZ')
        # date_time_obj += datetime.timedelta(hours=1)
        # time_future = date_time_obj.strftime("%Y-%m-%dT%H:%M:%SZ")
        # weather_info = requests.get("https://api.weather.gov/stations/%s/observations?start=%s&end=%s" % (nearest_station, timeOf, time_future))
        # weather_info = weather_info.json()


# Takes activity data, returns random funny holiday
def random_date_title(activity):
    month = activity["start_date"][5:7]
    day = activity["start_date"][8:10]

    if int(month) < 10:
        month = month[1]

    if int(day) < 10:
        day = day[1]

    events = dates.dates["%s/%s/20" % (month, day)]
    event_length = len(events)
    random_event = random.randint(0, event_length - 1)
    event = events[random_event]["title"]

    return event


# Takes user id, attempts to refresh their token. If success, returns 1, else returns nothing
def refresh_token(user_id):
    conn = mysql.connect()
    sql = 'SELECT * FROM users WHERE id = %s'
    val = (user_id)
    cursor = conn.cursor()
    cursor.execute(sql, val)

    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if time.time() >= user[3]:
        data = {
          'client_id': SECRETS.client_id,
          'client_secret': SECRETS.client_secret,
          'grant_type': 'refresh_token',
          'refresh_token': user[2]
        }

        response = requests.post('https://www.strava.com/api/v3/oauth/token', data=data)

        response = response.json()

        conn = mysql.connect()
        sql = 'UPDATE users SET token = %s, refresh_token = %s, expires = %s WHERE id = %s'
        val = (response["access_token"], response["refresh_token"], response["expires_at"], user_id)
        cursor = conn.cursor()
        cursor.execute(sql, val)
        conn.commit()

        cursor.close()
        conn.close()
    return 1


# Takes activity, determines if it fits long run, workout, or run. Also returns formated activity type if not 'Run'
def get_type(activity):
    if activity["type"] == "Run":
        mileage = activity["distance"] / 1600
        duration = activity["moving_time"] / 60
        pace = duration / mileage

        if pace <= 5.67:
            if duration < 96:
                return 'Workout'
            else:
                return 'Long Workout'
        elif duration >= 96:
            return 'Long Run'
        else:
            return 'Run'
    else:
        return re.sub(r"(?<=\w)([A-Z])", r" \1", activity["type"])


# Takes string, user token, and activity data. Sets title to given string and 50/50 chance to plug titles.run in description
def set_title(string, token, activity):
    if activity["description"]:
        description = activity["description"] + "\n"
    else:
        description = ""
    if random.randint(0,1) == 1:
        description += "Titled via titles.run"

    url = "https://www.strava.com/api/v3/activities/%s" % activity["id"]
    data = { "name": "%s" % string, "description": "%s" % description }
    headers = {'Authorization' : 'Bearer %s' % token}

    response = requests.put(url, headers=headers, data=data)

    print(str(response.json()["id"]) + ', ' + response.json()["name"])


if __name__ == '__main__':
    app.run()
