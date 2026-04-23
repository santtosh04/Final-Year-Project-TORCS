import socket
import sys
import getopt
import os
import time
import requests
import json
import re
import threading
import pyttsx3
import random
from queue import Queue


PI = 3.14159265359
last_commentary_time = 0
race_start_time = None
tts_idle_event = threading.Event()  # Set when TTS is idle
prev_state = None
LOG_FILE = "race_commentary.log"


speech_queue = Queue()
llm_queue = Queue()


# Instant commentary template
COMMENTARY_TEMPLATE = {
    "race_start": [
        "AND THEY'RE OFF! The lights go green and we are RACING! Engines roar, tires screech, and the adrenaline is sky high as the cars surge off the line in a thunderous start, every driver hungry for victory! The crowd is on its feet, anticipation turns to action as the field storms into the first corner.",
        "GREEN LIGHT! The race is ON! The field explodes into action, every driver fighting for position as the crowd erupts in excitement, the tension finally breaking as the cars rocket away from the grid! The atmosphere is electric, every second counts as they jostle for the lead.",
        "WE ARE ON! Engines roaring, the battle begins as the cars launch forward, jostling for every inch of track in a spectacular start, the grandstands shaking with excitement! The drivers are pushing to the limit, determined to seize every opportunity from the very first lap.",
        "THE FLAG DROPS! Let the race begin! The tension breaks as the cars accelerate away, each one hungry for victory, the air filled with the sound of pure speed and determination! The opening moments are crucial, and the drama is already unfolding.",
        "AND AWAY WE GO! This is going to be incredible! The drivers waste no time, charging into the first corner with fearless determination, the race is truly underway! The energy is palpable, and the battle for supremacy has officially started.",
        "LIGHTS OUT AND AWAY WE GO! The grandstands shake as the cars blast off the grid, the race is truly underway, and anything can happen from here! The roar of engines echoes across the circuit as the competitors dive into turn one, setting the stage for an epic showdown.",
    ],
}


def log_message(message):
    """Print message and write to log file with timestamp."""
    timestamp = time.strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{log_entry}\n")


# Ollama local configuration
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "granite3.1-moe:1b"

# ================= COMMENTARY FILTERING SYSTEM =================
# Fallback messages used when commentary is rejected
FALLBACK_MESSAGES = [
    "The driver pushes on, concentrating on the next corner.",
    "Smooth progress through this section of the track.",
    "Maintaining focus, looking for every tenth of a second.",
    "Steady on the throttle, seeking the perfect line.",
    "Focused and determined, pushing for consistency.",
    "Confidence through the apex, smooth and controlled.",
]

# Profanity and inappropriate language blocklist
PROFANITY_BLOCKLIST = [
    "damn", "hell", "crap", "sucks", "shit", "bullshit",
    "fuck", "ass", "bitch", "bastard", "piss", "dick",
]

# Keywords indicating hallucination (multiple drivers/rivals/teams/positions)
HALLUCINATION_KEYWORDS = [
    "rival", "rivals", "teammate", "teammates", "team", "teams",
    "competitor", "competitors", "opponent", "opponents",
    "leader", "leading", "second", "third", "fourth", "fifth",
    "1st", "2nd", "3rd", "position battle", "battle for",
    "chase", "chasing", "catching", "behind", "ahead",
    "pass", "passing", "overtake", "overtaking",
]

data_size = 2**17

ophelp = "Options:\n"
ophelp += " --host, -H <host>    TORCS server host. [localhost]\n"
ophelp += " --port, -p <port>    TORCS port. [3001]\n"
ophelp += " --id, -i <id>        ID for server. [SCR]\n"
ophelp += " --steps, -m <#>      Maximum simulation steps. 1 sec ~ 50 steps. [100000]\n"
ophelp += " --episodes, -e <#>   Maximum learning episodes. [1]\n"
ophelp += (
    " --track, -t <track>  Your name for this track. Used for learning. [unknown]\n"
)
ophelp += " --stage, -s <#>      0=warm up, 1=qualifying, 2=race, 3=unknown. [3]\n"
ophelp += " --debug, -d          Output full telemetry.\n"
ophelp += " --help, -h           Show this help.\n"
ophelp += " --version, -v        Show current version."
usage = "Usage: %s [ophelp [optargs]] \n" % sys.argv[0]
usage = usage + ophelp
version = "20130505-2"


def validate_commentary(commentary: str) -> tuple[bool, str]:
    """
    Validate commentary against formatting, hallucination, and content rules.
    Returns: (is_valid, reason_for_rejection)
    """
    if not commentary or not commentary.strip():
        return False, "Empty commentary"
    
    commentary_lower = commentary.lower()
    
    # Check for profanity
    for bad_word in PROFANITY_BLOCKLIST:
        if bad_word in commentary_lower:
            return False, f"Profanity detected: '{bad_word}'"
    
    # Check for hallucinations (other drivers/teams/positions)
    for keyword in HALLUCINATION_KEYWORDS:
        if keyword in commentary_lower:
            return False, f"Hallucination detected: mentions '{keyword}' (implying multiple drivers/teams)"
    
    # Check for multiple sentences (more than one period)
    sentence_count = commentary.count(".") + commentary.count("!") + commentary.count("?")
    if sentence_count > 1:
        return False, f"Multiple sentences detected ({sentence_count} terminal punctuation marks)"
    
    # Check for word count (should be <= 15 words)
    word_count = len(commentary.split())
    if word_count > 15:
        return False, f"Exceeds 15-word limit ({word_count} words)"
    
    # Check for list markers (-, *, numbers followed by period or dot)
    list_markers = ["- ", "* ", " - ", " * "]
    for marker in list_markers:
        if marker in commentary:
            return False, f"List marker detected: '{marker.strip()}'"
    
    # Check for numbered lists (1. 2. etc)
    if re.search(r"\d+\.", commentary):
        return False, "Numbered list detected"
    
    return True, ""


def filter_commentary(commentary: str) -> str:
    """
    Apply validation filter to commentary. Returns filtered commentary or fallback.
    If rejected, logs the violation and returns a random fallback message.
    """
    is_valid, rejection_reason = validate_commentary(commentary)
    
    if not is_valid:
        fallback = random.choice(FALLBACK_MESSAGES)
        log_message(
            f"[FILTER REJECTED] Original: '{commentary}' | Reason: {rejection_reason} | Fallback: '{fallback}'"
        )
        return fallback
    
    return commentary


def clip(v, lo, hi):
    if v < lo:
        return lo
    elif v > hi:
        return hi
    else:
        return v


def bargraph(x, mn, mx, w, c="X"):
    """Draws a simple asciiart bar graph. Very handy for
    visualizing what's going on with the data.
    x= Value from sensor, mn= minimum plottable value,
    mx= maximum plottable value, w= width of plot in chars,
    c= the character to plot with."""
    if not w:
        return ""  # No width!
    if x < mn:
        x = mn  # Clip to bounds.
    if x > mx:
        x = mx  # Clip to bounds.
    tx = mx - mn  # Total real units possible to show on graph.
    if tx <= 0:
        return "backwards"  # Stupid bounds.
    upw = tx / float(w)  # X Units per output char width.
    if upw <= 0:
        return "what?"  # Don't let this happen.
    negpu, pospu, negnonpu, posnonpu = 0, 0, 0, 0
    if mn < 0:  # Then there is a negative part to graph.
        if x < 0:  # And the plot is on the negative side.
            negpu = -x + min(0, mx)
            negnonpu = -mn + x
        else:  # Plot is on pos. Neg side is empty.
            negnonpu = -mn + min(0, mx)  # But still show some empty neg.
    if mx > 0:  # There is a positive part to the graph
        if x > 0:  # And the plot is on the positive side.
            pospu = x - max(0, mn)
            posnonpu = mx - x
        else:  # Plot is on neg. Pos side is empty.
            posnonpu = mx - max(0, mn)  # But still show some empty pos.
    nnc = int(negnonpu / upw) * "-"
    npc = int(negpu / upw) * c
    ppc = int(pospu / upw) * c
    pnc = int(posnonpu / upw) * "_"
    return "[%s]" % (nnc + npc + ppc + pnc)


class Client:
    def __init__(
        self, H=None, p=None, i=None, e=None, t=None, s=None, d=None, vision=False
    ):
        self.vision = vision

        self.host = "localhost"
        self.port = 3001
        self.sid = "SCR"
        self.maxEpisodes = 1  # "Maximum number of learning episodes to perform"
        self.trackname = "unknown"
        self.stage = 3  # 0=Warm-up, 1=Qualifying 2=Race, 3=unknown <Default=3>
        self.debug = False
        self.maxSteps = 100000  # 50steps/second
        self.parse_the_command_line()
        if H:
            self.host = H
        if p:
            self.port = p
        if i:
            self.sid = i
        if e:
            self.maxEpisodes = e
        if t:
            self.trackname = t
        if s:
            self.stage = s
        if d:
            self.debug = d
        self.S = ServerState()
        self.R = DriverAction()
        self.setup_connection()

    def setup_connection(self):
        try:
            self.so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error as emsg:
            print("Error: Could not create socket...")
            sys.exit(-1)
        self.so.settimeout(1)

        n_fail = 5
        while True:
            a = "-45 -19 -12 -7 -4 -2.5 -1.7 -1 -.5 0 .5 1 1.7 2.5 4 7 12 19 45"

            initmsg = "%s(init %s)" % (self.sid, a)

            try:
                self.so.sendto(initmsg.encode(), (self.host, self.port))
            except socket.error as emsg:
                sys.exit(-1)
            sockdata = str()
            try:
                sockdata, addr = self.so.recvfrom(data_size)
                sockdata = sockdata.decode("utf-8")
            except socket.error as emsg:
                print("Waiting for server on %d............" % self.port)
                print("Count Down : " + str(n_fail))
                if n_fail < 0:
                    print("relaunch torcs")
                    os.system("pkill torcs")
                    time.sleep(1.0)
                    if self.vision is False:
                        os.system("torcs -nofuel -nodamage -nolaptime &")
                    else:
                        os.system("torcs -nofuel -nodamage -nolaptime -vision &")

                    time.sleep(1.0)
                    os.system("sh autostart.sh")
                    n_fail = 5
                n_fail -= 1

            identify = "***identified***"
            if identify in sockdata:
                print("Client connected on %d.............." % self.port)
                break

    def parse_the_command_line(self):
        try:
            (opts, args) = getopt.getopt(
                sys.argv[1:],
                "H:p:i:m:e:t:s:dhv",
                [
                    "host=",
                    "port=",
                    "id=",
                    "steps=",
                    "episodes=",
                    "track=",
                    "stage=",
                    "debug",
                    "help",
                    "version",
                ],
            )
        except getopt.error as why:
            print("getopt error: %s\n%s" % (why, usage))
            sys.exit(-1)
        try:
            for opt in opts:
                if opt[0] == "-h" or opt[0] == "--help":
                    print(usage)
                    sys.exit(0)
                if opt[0] == "-d" or opt[0] == "--debug":
                    self.debug = True
                if opt[0] == "-H" or opt[0] == "--host":
                    self.host = opt[1]
                if opt[0] == "-i" or opt[0] == "--id":
                    self.sid = opt[1]
                if opt[0] == "-t" or opt[0] == "--track":
                    self.trackname = opt[1]
                if opt[0] == "-s" or opt[0] == "--stage":
                    self.stage = int(opt[1])
                if opt[0] == "-p" or opt[0] == "--port":
                    self.port = int(opt[1])
                if opt[0] == "-e" or opt[0] == "--episodes":
                    self.maxEpisodes = int(opt[1])
                if opt[0] == "-m" or opt[0] == "--steps":
                    self.maxSteps = int(opt[1])
                if opt[0] == "-v" or opt[0] == "--version":
                    print("%s %s" % (sys.argv[0], version))
                    sys.exit(0)
        except ValueError as why:
            print(
                "Bad parameter '%s' for option %s: %s\n%s"
                % (opt[1], opt[0], why, usage)
            )
            sys.exit(-1)
        if len(args) > 0:
            print("Superflous input? %s\n%s" % (", ".join(args), usage))
            sys.exit(-1)

    def get_servers_input(self):
        """Server's input is stored in a ServerState object"""
        if not self.so:
            return
        sockdata = str()

        while True:
            try:
                sockdata, addr = self.so.recvfrom(data_size)
                sockdata = sockdata.decode("utf-8")
            except socket.error as emsg:
                print(".", end=" ")
            if "***identified***" in sockdata:
                print("Client connected on %d.............." % self.port)
                continue
            elif "***shutdown***" in sockdata:
                print(
                    (
                        (
                            "Server has stopped the race on %d. "
                            + "You were in %d place."
                        )
                        % (self.port, self.S.d["racePos"])
                    )
                )
                self.shutdown()
                return
            elif "***restart***" in sockdata:
                print("Server has restarted the race on %d." % self.port)
                self.shutdown()
                return
            elif not sockdata:  # Empty?
                continue  # Try again.
            else:
                self.S.parse_server_str(sockdata)
                if self.debug:
                    sys.stderr.write("\x1b[2J\x1b[H")  # Clear for steady output.
                    print(self.S)
                break  # Can now return from this function.

    def respond_to_server(self):
        if not self.so:
            return
        try:
            message = repr(self.R)
            self.so.sendto(message.encode(), (self.host, self.port))
        except socket.error as emsg:
            print("Error sending to server: %s Message %s" % (emsg[1], str(emsg[0])))
            sys.exit(-1)
        if self.debug:
            print(self.R.fancyout())

    def shutdown(self):
        if not self.so:
            return
        print(
            (
                "Race terminated or %d steps elapsed. Shutting down %d."
                % (self.maxSteps, self.port)
            )
        )
        self.so.close()
        self.so = None


class ServerState:
    """What the server is reporting right now."""

    def __init__(self):
        self.servstr = str()
        self.d = dict()

    def parse_server_str(self, server_string):
        """Parse the server string."""
        self.servstr = server_string.strip()[:-1]
        sslisted = self.servstr.strip().lstrip("(").rstrip(")").split(")(")
        for i in sslisted:
            w = i.split(" ")
            self.d[w[0]] = destringify(w[1:])

    def __repr__(self):
        return self.fancyout()
        out = str()
        for k in sorted(self.d):
            strout = str(self.d[k])
            if type(self.d[k]) is list:
                strlist = [str(i) for i in self.d[k]]
                strout = ", ".join(strlist)
            out += "%s: %s\n" % (k, strout)
        return out

    def fancyout(self):
        """Specialty output for useful ServerState monitoring."""
        out = str()
        sensors = [  # Select the ones you want in the order you want them.
            "stucktimer",
            "fuel",
            "distRaced",
            "distFromStart",
            "opponents",
            "wheelSpinVel",
            "z",
            "speedZ",
            "speedY",
            "speedX",
            "targetSpeed",
            "rpm",
            "skid",
            "slip",
            "track",
            "trackPos",
            "angle",
        ]

        for k in sensors:
            if type(self.d.get(k)) is list:  # Handle list type data.
                if k == "track":  # Nice display for track sensors.
                    strout = str()
                    raw_tsens = ["%.1f" % x for x in self.d["track"]]
                    strout += (
                        " ".join(raw_tsens[:9])
                        + "_"
                        + raw_tsens[9]
                        + "_"
                        + " ".join(raw_tsens[10:])
                    )
                elif k == "opponents":  # Nice display for opponent sensors.
                    strout = str()
                    for osensor in self.d["opponents"]:
                        if osensor > 190:
                            oc = "_"
                        elif osensor > 90:
                            oc = "."
                        elif osensor > 39:
                            oc = chr(int(osensor / 2) + 97 - 19)
                        elif osensor > 13:
                            oc = chr(int(osensor) + 65 - 13)
                        elif osensor > 3:
                            oc = chr(int(osensor) + 48 - 3)
                        else:
                            oc = "?"
                        strout += oc
                    strout = " -> " + strout[:18] + " " + strout[18:] + " <-"
                else:
                    strlist = [str(i) for i in self.d[k]]
                    strout = ", ".join(strlist)
            else:  # Not a list type of value.
                if k == "gear":  # This is redundant now since it's part of RPM.
                    gs = "_._._._._._._._._"
                    p = int(self.d["gear"]) * 2 + 2  # Position
                    l = "%d" % self.d["gear"]  # Label
                    if l == "-1":
                        l = "R"
                    if l == "0":
                        l = "N"
                    strout = gs[:p] + "(%s)" % l + gs[p + 3 :]
                elif k == "damage":
                    strout = "%6.0f %s" % (
                        self.d[k],
                        bargraph(self.d[k], 0, 10000, 50, "~"),
                    )
                elif k == "fuel":
                    strout = "%6.0f %s" % (
                        self.d[k],
                        bargraph(self.d[k], 0, 100, 50, "f"),
                    )
                elif k == "speedX":
                    cx = "X"
                    if self.d[k] < 0:
                        cx = "R"
                    strout = "%6.1f %s" % (
                        self.d[k],
                        bargraph(self.d[k], -30, 300, 50, cx),
                    )
                elif k == "speedY":  # This gets reversed for display to make sense.
                    strout = "%6.1f %s" % (
                        self.d[k],
                        bargraph(self.d[k] * -1, -25, 25, 50, "Y"),
                    )
                elif k == "speedZ":
                    strout = "%6.1f %s" % (
                        self.d[k],
                        bargraph(self.d[k], -13, 13, 50, "Z"),
                    )
                elif k == "z":
                    strout = "%6.3f %s" % (
                        self.d[k],
                        bargraph(self.d[k], 0.3, 0.5, 50, "z"),
                    )
                elif k == "trackPos":  # This gets reversed for display to make sense.
                    cx = "<"
                    if self.d[k] < 0:
                        cx = ">"
                    strout = "%6.3f %s" % (
                        self.d[k],
                        bargraph(self.d[k] * -1, -1, 1, 50, cx),
                    )
                elif k == "stucktimer":
                    if self.d[k]:
                        strout = "%3d %s" % (
                            self.d[k],
                            bargraph(self.d[k], 0, 300, 50, "'"),
                        )
                    else:
                        strout = "Not stuck!"
                elif k == "rpm":
                    g = self.d["gear"]
                    if g < 0:
                        g = "R"
                    else:
                        g = "%1d" % g
                    strout = bargraph(self.d[k], 0, 10000, 50, g)
                elif k == "angle":
                    asyms = [
                        "  !  ",
                        ".|'  ",
                        "./'  ",
                        "_.-  ",
                        ".--  ",
                        "..-  ",
                        "---  ",
                        ".__  ",
                        "-._  ",
                        "'-.  ",
                        "'\.  ",
                        "'|.  ",
                        "  |  ",
                        "  .|'",
                        "  ./'",
                        "  .-'",
                        "  _.-",
                        "  __.",
                        "  ---",
                        "  --.",
                        "  -._",
                        "  -..",
                        "  '\.",
                        "  '|.",
                    ]
                    rad = self.d[k]
                    deg = int(rad * 180 / PI)
                    symno = int(0.5 + (rad + PI) / (PI / 12))
                    symno = symno % (len(asyms) - 1)
                    strout = "%5.2f %3d (%s)" % (rad, deg, asyms[symno])
                elif k == "skid":  # A sensible interpretation of wheel spin.
                    frontwheelradpersec = self.d["wheelSpinVel"][0]
                    skid = 0
                    if frontwheelradpersec:
                        skid = (
                            0.5555555555 * self.d["speedX"] / frontwheelradpersec
                            - 0.66124
                        )
                    strout = bargraph(skid, -0.05, 0.4, 50, "*")
                elif k == "slip":  # A sensible interpretation of wheel spin.
                    frontwheelradpersec = self.d["wheelSpinVel"][0]
                    slip = 0
                    if frontwheelradpersec:
                        slip = (
                            self.d["wheelSpinVel"][2] + self.d["wheelSpinVel"][3]
                        ) - (self.d["wheelSpinVel"][0] + self.d["wheelSpinVel"][1])
                    strout = bargraph(slip, -5, 150, 50, "@")
                else:
                    strout = str(self.d[k])
            out += "%s: %s\n" % (k, strout)
        return out


class DriverAction:
    """What the driver is intending to do (i.e. send to the server).
    Composes something like this for the server:
    (accel 1)(brake 0)(gear 1)(steer 0)(clutch 0)(focus 0)(meta 0) or
    (accel 1)(brake 0)(gear 1)(steer 0)(clutch 0)(focus -90 -45 0 45 90)(meta 0)"""

    def __init__(self):
        self.actionstr = str()
        self.d = {
            "accel": 0.2,
            "brake": 0,
            "clutch": 0,
            "gear": 1,
            "steer": 0,
            "focus": [-90, -45, 0, 45, 90],
            "meta": 0,
        }

    def clip_to_limits(self):
        """There pretty much is never a reason to send the server
        something like (steer 9483.323). This comes up all the time
        and it's probably just more sensible to always clip it than to
        worry about when to. The "clip" command is still a snakeoil
        utility function, but it should be used only for non standard
        things or non obvious limits (limit the steering to the left,
        for example). For normal limits, simply don't worry about it."""
        self.d["steer"] = clip(self.d["steer"], -1, 1)
        self.d["brake"] = clip(self.d["brake"], 0, 1)
        self.d["accel"] = clip(self.d["accel"], 0, 1)
        self.d["clutch"] = clip(self.d["clutch"], 0, 1)
        if self.d["gear"] not in [-1, 0, 1, 2, 3, 4, 5, 6]:
            self.d["gear"] = 0
        if self.d["meta"] not in [0, 1]:
            self.d["meta"] = 0
        if (
            type(self.d["focus"]) is not list
            or min(self.d["focus"]) < -180
            or max(self.d["focus"]) > 180
        ):
            self.d["focus"] = 0

    def __repr__(self):
        self.clip_to_limits()
        out = str()
        for k in self.d:
            out += "(" + k + " "
            v = self.d[k]
            if not type(v) is list:
                out += "%.3f" % v
            else:
                out += " ".join([str(x) for x in v])
            out += ")"
        return out
        return out + "\n"

    def fancyout(self):
        """Specialty output for useful monitoring of bot's effectors."""
        out = str()
        od = self.d.copy()
        od.pop("gear", "")  # Not interesting.
        od.pop("meta", "")  # Not interesting.
        od.pop("focus", "")  # Not interesting. Yet.
        for k in sorted(od):
            if k == "clutch" or k == "brake" or k == "accel":
                strout = ""
                strout = "%6.3f %s" % (od[k], bargraph(od[k], 0, 1, 50, k[0].upper()))
            elif k == "steer":  # Reverse the graph to make sense.
                strout = "%6.3f %s" % (od[k], bargraph(od[k] * -1, -1, 1, 50, "S"))
            else:
                strout = str(od[k])
            out += "%s: %s\n" % (k, strout)
        return out


def destringify(s):
    """makes a string into a value or a list of strings into a list of
    values (if possible)"""
    if not s:
        return s
    if type(s) is str:
        try:
            return float(s)
        except ValueError:
            print("Could not find a value in %s" % s)
            return s
    elif type(s) is list:
        if len(s) < 2:
            return destringify(s[0])
        else:
            return [destringify(i) for i in s]


#############################################
# MODULAR DRIVE LOGIC WITH USER PARAMETERS  #
#############################################

# ================= USER CONFIGURABLE PARAMETERS =================
STEER_ANGLE_GAIN = (
    25  # How much to steer based on car's angle to track (reduced to prevent overshoot)
)
CENTERING_GAIN = (
    0.35  # How much to steer toward center of track (increased to stay away from walls)
)
LOOK_AHEAD_GAIN = 0.25  # How much to pre-steer for upcoming corners (increased)

SPEED_TARGETS = {
    150: 280,  # Can see 150m+ = fast
    100: 220,  # Can see 100m+ = medium-fast
    60: 180,  # Can see 60m+  = medium
    40: 150,  # Can see 40m+  = slower
    25: 120,  # Can see 25m+  = slow
    15: 80,  # Can see 15m+  = very slow
    0: 60,  # Hairpin       = careful
}

GEAR_SPEEDS = [0, 40, 75, 110, 150, 190]
GEAR_HYSTERESIS = 15
current_gear = 1


# ================= HELPER FUNCTIONS =================
def calculate_steering(S):
    track = S.get("track", [200] * 19)
    steer = S["angle"] * STEER_ANGLE_GAIN / PI - S["trackPos"] * CENTERING_GAIN

    left_distance = (track[15] + track[16] + track[17]) / 3
    right_distance = (track[1] + track[2] + track[3]) / 3
    curve_indicator = left_distance - right_distance

    front_distance = track[9]
    if front_distance < 200:
        urgency = max(0, (200 - front_distance) / 200)
        curve_steer = (curve_indicator / 100) * LOOK_AHEAD_GAIN * urgency
        steer += curve_steer

    trackPos = S["trackPos"]
    if abs(trackPos) > 0.7:
        edge_factor = (abs(trackPos) - 0.7) / 0.3
        edge_correction = edge_factor * 0.5
        if trackPos > 0:
            steer -= edge_correction
        else:
            steer += edge_correction

    return steer


def get_target_speed(S):
    track = S.get("track", [200] * 19)
    front = track[9]
    for distance, speed in sorted(SPEED_TARGETS.items(), reverse=True):
        if front > distance:
            return speed
    return SPEED_TARGETS[0]  # Minimum speed


def calculate_throttle(S, R):
    speed = S["speedX"]
    target = get_target_speed(S)

    if speed < 30:
        return 1.0

    if speed < target + 10:
        return 1.0  # Full throttle until over target
    else:
        return 0.5  # Still pushing when over


def apply_brakes(S):

    speed = S["speedX"]
    target = get_target_speed(S)
    trackPos = abs(S["trackPos"])
    angle = abs(S["angle"])
    track = S.get("track", [200] * 19)

    brake = 0.0

    front = track[9]  # Straight ahead

    if front < 70 and speed > 140:
        urgency = (70 - front) / 70  # 0 to 1
        speed_factor = (speed - 140) / 100
        brake = min(0.5, urgency * speed_factor * 0.6)

    if speed > target + 5:
        over_speed = speed - target - 5
        brake = max(brake, min(0.4, over_speed / 60))

    if trackPos > 0.8 and speed > 100:
        brake = max(brake, 0.5)

    if angle > 0.3 and speed > 100:
        brake = max(brake, 0.5)

    return brake


def shift_gears(S):
    global current_gear
    speed = S["speedX"]

    if current_gear < 6 and speed > GEAR_SPEEDS[current_gear]:
        current_gear += 1

    elif current_gear > 1 and speed < GEAR_SPEEDS[current_gear - 1] - GEAR_HYSTERESIS:
        current_gear -= 1

    return current_gear


def get_granite_commentary(telemetry, event=None):
    """
    Send telemetry to Granite for race analysis and procedural commentary.
    """
    speed = telemetry["speedX"]
    track_pos = telemetry["trackPos"]
    track = telemetry.get("track", [200] * 19)
    front_distance = track[9] if len(track) > 9 else 200

    # Build situational context
    situation = (
        "straight"
        if front_distance > 100
        else "approaching corner" if front_distance > 40 else "in corner"
    )
    position = (
        "center"
        if abs(track_pos) < 0.3
        else "left side" if track_pos > 0 else "right side"
    )

    # For logging telemetry snapshot
    telemetry_snapshot = f"Telemetry: Speed: {speed:.0f} km/h, Track position: {position}, Situation: {situation}"

    prompt = f"""
    You are a professional race commentator describing a SOLO time trial lap.

    IMPORTANT RULES:
    - There is ONLY ONE car on track
    - DO NOT mention other drivers, teams, or positions
    - OUTPUT EXACTLY ONE commentary line
    - NO lists, NO numbering

    Style:
    - max 15 words
    - natural and exciting
    - focus on ONE key moment

    Here are examples of correct behavior:

    Telemetry:
    Speed: 200 km/h
    Track position: center
    Situation: straight
    Commentary: The car rockets down the straight at full throttle!

    Telemetry:
    Speed: 140 km/h
    Track position: left side
    Situation: approaching corner
    Commentary: He turns in smoothly, hugging the racing line!

    Telemetry:
    Speed: 110 km/h
    Track position: right side
    Situation: in corner
    Commentary: He runs wide but keeps the car under control!

    Now generate commentary for this telemetry:

    Telemetry:
    Speed: {speed:.0f} km/h
    Track position: {position}
    Situation: {situation}

    Commentary:
    """

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }

    try:
        t_start = time.time()
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=30
        )
        t_end = time.time()
        resp.raise_for_status()
        data = resp.json()
        response_time = t_end - t_start
        # Log the telemetry snapshot and response time
        log_message(
            f"{telemetry_snapshot} | Granite response time: {response_time:.3f}s"
        )
        return data["response"].strip()

    except requests.exceptions.ConnectionError as e:
        log_message(
            f"[LLM ERROR] ConnectionError: Ollama service unreachable at {OLLAMA_BASE_URL} - {e}"
        )
    except requests.exceptions.Timeout as e:
        log_message(
            f"[LLM ERROR] Timeout: LLM request exceeded 30-second timeout - {e}"
        )
    except requests.exceptions.HTTPError as e:
        log_message(f"[LLM ERROR] HTTPError: Ollama API returned non-200 status - {e}")
    except json.JSONDecodeError as e:
        log_message(
            f"[LLM ERROR] JSONDecodeError: Failed to parse Ollama response as valid JSON - {e}"
        )
    except Exception as e:
        log_message(
            f"[LLM ERROR] Unexpected error during LLM request: {type(e).__name__} - {e}"
        )

    return ""


def rule_based_commentary(speed_kmh: float, position: str, situation: str) -> str:

    # ----- STRAIGHT SITUATION -----
    if situation == "straight":
        if speed_kmh > 200:
            return "Rocketing down the straight at phenomenal speed!"
        elif speed_kmh > 150:
            return "Down the straight, carrying excellent velocity."
        elif speed_kmh > 100:
            return "Making good progress on the straight."
        else:
            return "Cruising through the straight section."

    # ----- APPROACHING CORNER -----
    elif situation == "approaching corner":
        if position == "left side":
            if speed_kmh > 150:
                return "Hard on the brakes, turning in from the left."
            else:
                return "Setting up for the left-hander, hugging the inside."
        elif position == "right side":
            if speed_kmh > 150:
                return "Late apex approach from the right side."
            else:
                return "Smooth entry from the right, preparing to turn."
        else:  # centre
            if speed_kmh > 160:
                return "Approaching the corner quickly, still centred."
            else:
                return "Steady approach, now turning in."

    # ----- IN CORNER -----
    elif situation == "in corner":
        if speed_kmh < 80:
            return "Very tight through the corner, nearly crawling."
        elif speed_kmh < 120:
            if position == "left side":
                return "Holding the inside line, very controlled."
            elif position == "right side":
                return "Running a bit wide but keeping it tidy."
            else:
                return "Through the apex, balanced and smooth."
        else:  # high speed in corner
            return "Pushing hard, lots of commitment through the bend."


# ================= MAIN DRIVE FUNCTION =================
def drive_modular(c):
    S, R = c.S.d, c.R.d

    R["steer"] = calculate_steering(S)
    R["accel"] = calculate_throttle(S, R)
    R["brake"] = apply_brakes(S)
    R["gear"] = shift_gears(S)

    return


def fetch_and_log_commentary(telemetry):
    commentary = get_granite_commentary(telemetry)
    if commentary:
        # Apply filter to commentary
        filtered_commentary = filter_commentary(commentary)
        # Log the commentary (timestamp is added in log_message)
        log_message(f"Commentary: {filtered_commentary}")
        speech_queue.put(filtered_commentary)


def llm_commentary_worker():
    while True:
        telemetry = llm_queue.get()
        commentary = get_granite_commentary(telemetry)
        if commentary:
            # Apply filter to commentary
            filtered_commentary = filter_commentary(commentary)
            log_message(f"Commentary: {filtered_commentary}")
            speech_queue.put(filtered_commentary)


def start_commentary_thread(telemetry):
    llm_queue.put(telemetry.copy())


def commentary_controller(server_state):
    global last_commentary_time, race_start_time

    now = time.time()
    if race_start_time is None:
        race_start_time = now
    delta = now - last_commentary_time
    since_race_start = now - race_start_time
    if since_race_start < 5:
        return
    if delta > 10:
        last_commentary_time = now
        start_commentary_thread(server_state)


def process_speech():
    while True:
        text = speech_queue.get()
        try:
            # Re-initialize TTS engine for each utterance to avoid thread issues
            local_engine = pyttsx3.init()
            voices = local_engine.getProperty("voices")
            local_engine.setProperty("voice", voices[2].id)
            local_engine.setProperty("rate", 195)
            local_engine.say(text)
            local_engine.runAndWait()
            local_engine.stop()
            del local_engine
        except Exception as e:
            print(f"[TTS ERROR] Exception for text: {repr(text)}")
        finally:
            tts_idle_event.set()


# ================= MAIN LOOP =================
if __name__ == "__main__":
    # Clear/create log file at startup
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(
            f"=== TORCS Race Commentary Log - {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n"
        )

    C = Client(p=3001)
    tts_idle_event.set()  # TTS is idle at startup
    threading.Thread(target=process_speech, daemon=True).start()
    threading.Thread(target=llm_commentary_worker, daemon=True).start()

    # Opening commentary - gets the audience hyped!
    opening_line = random.choice(COMMENTARY_TEMPLATE["race_start"])
    log_message(opening_line)
    speech_queue.put(opening_line)

    C.get_servers_input()
    start_commentary_thread(C.S.d)

    last_loop_time = time.time()
    for step in range(C.maxSteps, 0, -1):
        loop_start = time.time()
        C.get_servers_input()
        drive_modular(C)
        commentary_controller(C.S.d)
        C.respond_to_server()
        loop_end = time.time()
        loop_duration = loop_end - loop_start
        if loop_duration > 0.1:
            print(f"[DEBUG] Main loop slow: {loop_duration:.3f}s at step {step}")
