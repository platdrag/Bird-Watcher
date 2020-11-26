from imutils.video import VideoStream
import argparse
import datetime
import imutils
import time
import cv2
import collections
from functools import reduce
import gphoto2 as gp
#from camera_control import CameraControlManagerSubProcess, CameraControlMsg, release_camera, CAPTURE_IMAGE, DOWNLOAD_IMAGE, DEFAULT_CAPTURE_TARGET, DEFAULT_DOWNLOAD_PHOTO_FOLDER
import concurrent.futures
import numpy as np 
import yaml 
from motion_detector import MotionDetector
from defaults import *
import threading

from flask import Flask, Response, request, render_template

flask_app = Flask(__name__)

@flask_app.route("/video_feed_frame")
def video_feed_frame():
	return Response( flask_app.md.stream_original_frame(), mimetype = "multipart/x-mixed-replace; boundary=frame")

@flask_app.route("/status_text")
def status_text():
	return Response( flask_app.md.stream_status(), mimetype = "text/event-stream")

@flask_app.route("/get_coord")
def get_coord():
	x = request.args.get('x', 0, type=int)
	y = request.args.get('y', 0, type=int)
	flask_app.md.set_detect_rect(y,x)
	
	return Response( "ok", mimetype = "text/html")


@flask_app.route("/")
def index(): 
	# return the rendered template
	return render_template("index.html",status_text=flask_app.md.currentStatus)

import socket
def get_outbound_ip():
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	s.connect(("8.8.8.8", 80))
	return s.getsockname()[0]

if __name__ == "__main__":
	ap = argparse.ArgumentParser(
		   formatter_class=lambda prog: argparse.ArgumentDefaultsHelpFormatter(
                    prog, max_help_position=80, width=150))
		

	ap.add_argument("-v", "--video", default=None, help="path to the video file. leave empty for live feed")
	ap.add_argument("-x", "--capture-center-x", type=int, default=None, help="x coordinate - center of capture square")
	ap.add_argument("-y", "--capture-center-y", type=int, default=None, help="y coordinate - center of capture square")
	ap.add_argument("--triggered-area-percent", type=float, default=DEFAULT_TRIGGERED_AREA_PERCENT, help="minimum percentage of captured square to trigger motion detection")
	ap.add_argument("--capture-square-side", type=int, default=DEFAULT_CAPTURE_RECT_SIDE, help="side length of the capture square (area will be side*side)")
	ap.add_argument("--frames-to-trigger", type=int, default=DEFAULT_FRAMES_TO_TRIGGER, help="Number of frames motion is detected in before camera capture is triggered")
	ap.add_argument("--retrigger-interval", type=int, default=DEFFAULT_RETRIGGER_INTERVAL_SEC, help="Seconds to trigger another capture if detection is continous")
	ap.add_argument("--capture-target", type=int, default=DEFAULT_CAPTURE_TARGET, help="Location of photos saved on camera. 0=internal memory (faster), 1=SD Card")
	ap.add_argument("--frame-resize", type=int, default=DEFAULT_FRAME_RESIZE, help="resize live feed camera. None is not to resize")
	ap.add_argument("--download-photo-folder", type=str, default=DEFAULT_DOWNLOAD_PHOTO_FOLDER, help="Location of downloaded photos from camera")
	ap.add_argument("--autofocus-before-trigger",default=DEFAULT_AUTOFOCUS_BEFORE_TRIGGER, action="store_false", help="trigger camera's autofocus before capturign an image")
	ap.add_argument("--ui-port",type=int, default=DEFAULT_UI_PORT, help="UI web server listening port")
	

	args = vars(ap.parse_args())
	print (args)
	port = args.pop('ui_port')
	# loop over the frames of the video
	with MotionDetector(**args) as md:
		flask_app.md = md
		thread = threading.Thread(target = md.stream, args = (), daemon=True)
		thread.start()
		print ("running flask")
		ip = get_outbound_ip()
		flask_app.run(host=ip, port=port, debug=False, threaded=True, use_reloader=False)
		
