from imutils.video import VideoStream
import datetime
import imutils
import time
import cv2
import collections
from functools import reduce
import gphoto2 as gp
from camera_control import CameraControlManagerSubProcess, CameraControlMsg, release_camera, CAPTURE_IMAGE
import concurrent.futures
import numpy as np 
import yaml 
import threading
from defaults import *

class MotionDetector:
	def __init__(self,video, 
				triggered_area_percent = DEFAULT_TRIGGERED_AREA_PERCENT, 
				frame_resize = DEFAULT_FRAME_RESIZE, 
				capture_square_side = DEFAULT_CAPTURE_RECT_SIDE, 
				retrigger_interval = DEFFAULT_RETRIGGER_INTERVAL_SEC,
				frames_to_trigger = DEFAULT_FRAMES_TO_TRIGGER, 
				rebase_interval = REBASE_INTERVAL, 
				download_photo_folder = DEFAULT_DOWNLOAD_PHOTO_FOLDER,
				autofocus_before_trigger = DEFAULT_AUTOFOCUS_BEFORE_TRIGGER,
				capture_target = DEFAULT_CAPTURE_TARGET,
				capture_center_x = None, capture_center_y = None):
		#set all constructor args as member of class
		self.__dict__.update(locals()) 
		self.min_triggered_area = triggered_area_percent * capture_square_side ** 2
		self.firstFrame = None
		self.conf = yaml.safe_load(open(CONF_FILE))
		self.conf['coordinates']['x'] = capture_center_x if capture_center_x else self.conf['coordinates']['x']
		self.conf['coordinates']['y'] = capture_center_y if capture_center_y else self.conf['coordinates']['y']
		self.currentFrames = None
		self.currentStatus = 'Undetected'
		self.status_change_event = threading.Event()
		
	def __enter__(self):
		# initialize the first frame in the video stream
		# if the video argument is None, then we are reading from webcam	
		if self.video is None:
			self.vs = VideoStream(src=0).start()
			time.sleep(0.5)
			self.streaming = True
		# otherwise, we are reading from a video file
		else:
			self.vs = cv2.VideoCapture(self.video)
			self.streaming = False
		
		frame = self._read_frame()
		self.frame_dim = frame.shape
		self.set_detect_rect(self.conf['coordinates']['x'],self.conf['coordinates']['y']) #set detection rect to middle of frame of size detection_rect_len
		

		return self

	def __exit__(self, type, value, traceback):
		# cleanup the camera and close all streams
		self.vs.stop() if self.streaming else self.vs.release()
		release_camera(self.camera)
		cv2.destroyAllWindows()
		self.currentFrames = None
		print ("exit. all clear. bye...")

	
	
	
	def _set_current_status(self, new_status):
		if new_status != self.currentStatus:
			self.currentStatus = new_status
			self.status_change_event.set()
			print ('status change:',new_status)
	
	#grab the current frame 
	def _read_frame(self):
		frame = self.vs.read()
		frame = frame if self.streaming else frame[1]
		frame = frame if self.frame_resize is None else imutils.resize(frame, width=self.frame_resize) # resize the frame
		return frame

	def _encode_frame(self):
		h=[x.shape[0] for x in self.currentFrames]
		w=[x.shape[1] for x in self.currentFrames]
		# Merging all frames into one image
		vis = np.vstack((self.currentFrames[0], cv2.cvtColor(self.currentFrames[1],cv2.COLOR_GRAY2BGR),cv2.cvtColor(self.currentFrames[2],cv2.COLOR_GRAY2BGR)))
		fill = np.zeros((self.currentFrames[3].shape[0], vis.shape[1]+20, 3), np.uint8)
		fill[:,:,:] = (128,128,128)
		fill[:vis.shape[0], 10:vis.shape[1]+10, :3]=vis
		vis = np.hstack((self.currentFrames[3],fill))

		(flag, encodedImage) = cv2.imencode(".jpg", vis)
		return b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + bytearray(encodedImage) + b'\r\n'

	def set_detect_rect(self, x = None,y = None):
		x = int(self.frame_dim[0] / 2) if x is None else x
		y = int(self.frame_dim[1] / 2) if y is None else y 
		drl = int (self.capture_square_side / 2)
		self.detect_rect_slice = ( slice(
			max(x - drl, 0),
			min(x + drl, self.frame_dim[0])),
		slice(
			max(y - drl, 0),
			min(y + drl, self.frame_dim[1]))
		)
		
		self.conf['coordinates']['x']=x
		self.conf['coordinates']['y']=y
		with open(CONF_FILE, 'w') as f:
			yaml.safe_dump(self.conf, f)
			
		#Setting timer to rebase the underlying background image used for moition detection comparison, to avoid drift.
		self.rebase_timer = threading.Timer(self.rebase_interval, self.set_detect_rect,[self.conf['coordinates']['x'],self.conf['coordinates']['y']]).start()

		self.firstFrame = None #need to retake the base image
		self._set_current_status(f'detection square was set to ({y},{x})')

	def stream (self):
		with CameraControlManagerSubProcess('worker-1', target_folder = self.download_photo_folder, capture_target = self.capture_target) as camCtl:    	
			# Init:
			last_detected = collections.deque(self.frames_to_trigger*[0], self.frames_to_trigger) # sliding window of last frames_to_trigger frames
			prev_triggered = False
			fc=1
			ts = time.time()
			frame_time = time.time()
			curr_fps = TARGET_FPS
			while True:
				if time.time() - frame_time < 1 / TARGET_FPS: # should be a bit less than 1/32th of a second
					time.sleep(0.003)
					continue
				frame_time = time.time()

				# grab the current frame and initialize the occupied/Undetected text		
				# read next frame from video source
				frame = self._read_frame()
				# if the frame could not be grabbed, then we have reached the end of the video
				if frame is None:
					break
				text = "Undetected"
				# saving original frame to show on video feed
				orig_frame = frame
				# slice the frame to the detection area only.
				frame = frame[self.detect_rect_slice]
				# convert frame to grayscale, and blur it
				gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
				gray = cv2.GaussianBlur(gray, (21, 21), 0)

				# if the first frame is None, initialize it
				if self.firstFrame is None:
					self.firstFrame = gray
					prev_triggered = False
					self._set_current_status('rebasing reference frame')
					continue

				# compute the absolute difference between the current frame and first frame
				frameDelta = cv2.absdiff(self.firstFrame, gray)
				thresh = cv2.threshold(frameDelta, 25, 255, cv2.THRESH_BINARY)[1]
				# dilate the thresholded image to fill in holes, then find contours on thresholded image
				thresh = cv2.dilate(thresh, None, iterations=2)
				cnts = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL,
					cv2.CHAIN_APPROX_SIMPLE)
				cnts = imutils.grab_contours(cnts)

				# loop over the contours
				valid_cnts = 0
				for c in cnts:
					# if the contour is too small, ignore it
					if cv2.contourArea(c) < self.min_triggered_area:
						continue
					valid_cnts += 1
					# compute the bounding box for the contour, draw it on the frame, and update the text
					(x, y, w, h) = cv2.boundingRect(c)
					cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
					text = "Movement Detected"			
				last_detected.append (valid_cnts)


				# Trigger handling: if motion was detected for NUM_FRAMES_TO_TRIGGER frames and then continously for RETRIGGER_INTERVAL seconds
				triggered = reduce ((lambda x,y: x>0 and y>0), last_detected) #triggered iff all last NUM_FRAMES_TO_TRIGGER motion was detected
				if triggered:
					if not prev_triggered:
						text='triggered!!!'
						camCtl.submit_task(CameraControlMsg(CAPTURE_IMAGE, {'autofocus':self.autofocus_before_trigger})) # fire camera
						triggered_time = frame_time #Setting base time for retrigger
					elif frame_time - triggered_time > self.retrigger_interval:
						text='re-triggered!!!'
						camCtl.submit_task(CameraControlMsg(CAPTURE_IMAGE, {'autofocus':self.autofocus_before_trigger})) # fire camera
						triggered_time = frame_time #re-Setting base time for retrigger
				prev_triggered = triggered
				
				#FPS calculation
				fc+=1
				if time.time() - ts > 1:
					curr_fps=fc
					fc=1
					ts=time.time()

				#drawing a square around the detection area in original frame				
				f=self.detect_rect_slice[0]
				t=self.detect_rect_slice[1]
				cv2.rectangle(orig_frame, (t.start-1,f.start-1),(t.stop+1,f.stop+1), (0, 255, 0), 1)
				cv2.putText(orig_frame,f"FPS: {curr_fps}", (10, orig_frame.shape[0] - 10),	cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)


				self.currentFrames=(frame, thresh, frameDelta, orig_frame)
				self._set_current_status(text)
					

	def stream_original_frame(self):
		while self.currentFrames:	
			yield self._encode_frame()


	def stream_status(self):
		while True:
			# print ('waiting for status')
			self.status_change_event.wait()
			# print ('sending new status',self.currentStatus)
			t=time.strftime("%y-%m-%d %H:%M:%S")
			yield f"data: {t}:{self.currentStatus}\n\n"
			self.status_change_event.clear()			


