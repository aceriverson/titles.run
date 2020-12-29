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

token = None
users = {}


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
        if activity["aspect_type"] == 'create':
            if refresh_token(activity["owner_id"]):

                conn = mysql.connect()
                cursor = conn.cursor()
                sql = 'SELECT token FROM users WHERE id = %s'
                val = (activity["owner_id"])
                cursor.execute(sql, val)
                user_token = cursor.fetchone()[0]
                cursor.close()
                conn.close()

                activity_data = get_activity(user_token, activity["object_id"])
                if activity_data["type"] == "Run":
                    rT = run_type(activity_data)
                    hilly = get_elevation(activity_data)
                else:
                    rT = re.sub(r"(?<=\w)([A-Z])", r" \1", activity_data["type"])
                    hilly = ""
                if not activity_data["upload_id"]:
                    rD = random_date_title(activity_data)

                    set_title(rD + ' ' + rT, user_token, activity_data)
                    return '', 200
                else:
                    location_conditions = get_weather(activity_data["start_latlng"], activity_data["start_date"])
                    relevant_location = get_poi(user_token, activity_data)
                    segments = get_crs(activity_data)

                    conditions_string = ""
                    if location_conditions[1]:
                        for cond in location_conditions[1]:
                            conditions_string += (cond + "y ")
                    else:
                        conditions_string = hilly

                    title_string = ""
                    # If POI is found, conditions (rainy, snowy...) + run type + "at location"
                    if relevant_location != "":
                        title_string = conditions_string + rT + " at " + relevant_location
                    # If no POI found, conditions (rainy, snowy...) + city + run type
                    else:
                        title_string = conditions_string + location_conditions[0] + rT

                    # If any top 5 on segments, segment name string (Pilot-Knob-Akin...) + "Segment Hunt"
                    if segments != "":
                        title_string = conditions_string + segments + " Segment Hunt"

                    set_title(title_string, user_token, activity_data)

        return '', 200



    elif request.method == 'GET':
        response = request.args.to_dict()
        print(request.args)

        try:
            if response["hub.verify_token"] == SECRETS.verify_token:
                print('returning')
                return '{"hub.challenge": "%s"}' % response["hub.challenge"], 200
        except:
            return "You aren't supposed to be here"


def get_activity(token, activity):
    print('getting activity')
    url = "https://www.strava.com/api/v3/activities/%s" % activity
    headers = {'Authorization' : 'Bearer %s' % token}

    response = requests.get(url, headers=headers)

    return(response.json())


def get_coord_stream(token, activity):
    headers = {'Authorization' : 'Bearer %s' % token}
    url = "https://www.strava.com/api/v3/activities/%s/streams?keys=latlng&key_by_type=true" % activity

    r = requests.get(url, headers=headers)

    return r.json()


def get_crs(activity):
    course_records = []
    for segment in activity["segment_efforts"]:
        try:
            if segment["achievements"][0]["type"] == "overall" and segment["achievements"][0]["rank"] <= 5:
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


def get_elevation(activity):
    if activity["total_elevation_gain"] / activity["distance"] >= 0.0125:
        return "Hilly "
    else:
        return ""


def get_poi(token, activity):
    coord_stream = get_coord_stream(token, activity["id"])
    polyline = fp.encode(coord_stream["latlng"]["data"])

    r = requests.get("https://browse.search.hereapi.com/v1/browse?apiKey=%s&at=%s,%s&route=%s;w=1000&categories=350,550-5510-0359&limit=10" % (SECRETS.here_key, activity["start_latitude"], activity["start_longitude"], polyline))
    r = r.json()
    relevant_location = {"name": "", "references": 0}
    for location in r["items"]:
        try:
            if len(location["references"]) > relevant_location["references"]:
                relevant_location["name"] = location["title"]
                relevant_location["references"] = len(location["references"])
        except:
            None

    return relevant_location["name"]


def get_weather(latlng, timeOf):
    try:
        point_info = requests.get("https://api.weather.gov/points/%s,%s" % (latlng[0], latlng[1]))
        point_info = point_info.json()
        stations = requests.get(point_info["properties"]["observationStations"])
        stations = stations.json()
        nearest_station = stations["features"][0]["properties"]["stationIdentifier"]

        date_time_obj = datetime.datetime.strptime(timeOf, '%Y-%m-%dT%H:%M:%SZ')
        date_time_obj += datetime.timedelta(hours=1)
        time_future = date_time_obj.strftime("%Y-%m-%dT%H:%M:%SZ")
        weather_info = requests.get("https://api.weather.gov/stations/%s/observations?start=%s&end=%s" % (nearest_station, timeOf, time_future))
        weather_info = weather_info.json()
        try:
            conditions = re.findall("Rain|Snow|Wind", weather_info["features"][0]["properties"]["textDescription"])
            return [point_info["properties"]["relativeLocation"]["properties"]["city"] + " ", conditions]
        except:
            try:
                return [point_info["properties"]["relativeLocation"]["properties"]["city"] + " ", None]
            except:
                return [None, None]
    except:
        return [None, None]


def random_date_title(activity):
    month = activity["start_date"][5:7]
    day = activity["start_date"][8:10]

    events = dates.dates["%s/%s/20" % (month, day)]
    event_length = len(events)
    random_event = random.randint(0, event_length - 1)
    event = events[random_event]["title"]

    return event


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


def run_type(activity):
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


@app.route('/trigger')
def trigger():
    return get_activity('bb227c306cdd18e5ec515a1f6159e2bc60de7bb6', request.args["id"])


if __name__ == '__main__':
    app.run()


