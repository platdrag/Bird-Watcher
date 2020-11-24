import io
import logging
import os
import subprocess
import sys
import time
import cv2
import gphoto2 as gp
import imutils
import numpy as np
from PIL import Image
import asyncio
import concurrent.futures
import threading
import traceback
from multiprocessing import Process
from multiprocessing.managers import SyncManager
from queue import PriorityQueue
from enum import Enum
from dataclasses import dataclass, field
from typing import Any
from defaults import *

RELEASE_CAMERA = 10
INIT_CAMERA = 20
CAPTURE_IMAGE = 30
DOWNLOAD_IMAGE = 40

@dataclass(order=True)
class CameraControlMsg:
    cmd: int
    args: Any=field(compare=False)


class CameraControlManagerSubProcess(SyncManager):
    ''' Camera control using a subprocess to avoid blocking of main event loop (threads are not enough)
        Implements communication using a shared process priority queue, so that capturing events will take priority over downloading, which will
        occur in the backgroud while no other task is being processed
    '''
    def __init__(self, name, target_folder = DEFAULT_DOWNLOAD_PHOTO_FOLDER, capture_target = DEFAULT_CAPTURE_TARGET):
        SyncManager.__init__(self) 
        CameraControlManagerSubProcess.register("PriorityQueue", PriorityQueue)  # Register a shared PriorityQueue
        self.name=name
        self.target_folder = target_folder
        self.capture_target=capture_target

    def __enter__(self):
        print ('in __init__')
        self.start()
        self.pq = self.PriorityQueue()
        self.worker_process = Process(target = self.worker, args = (), daemon=False)
        self.worker_process.start()       
        self.submit_task(CameraControlMsg(INIT_CAMERA, self.capture_target))

        return self

    def __exit__(self, type, value, traceback):
        print ('in __exit__')          
        self.submit_task(CameraControlMsg(RELEASE_CAMERA, None))
        self.pq.join()


    def worker(self):
        print ('worker running')
        while True:
            camMsg = self.pq.get()
            try:
                print ('got task',camMsg)
                if INIT_CAMERA == camMsg.cmd:
                    camera = init_camera(capture_target = camMsg.args)

                elif CAPTURE_IMAGE == camMsg.cmd:
                    file_path = capture_image(camera, **camMsg.args)
                    file_path = os.path.join(file_path.folder, file_path.name)
                    self.submit_task(CameraControlMsg(DOWNLOAD_IMAGE, {'file_path': file_path}))

                elif DOWNLOAD_IMAGE == camMsg.cmd:
                    download_image(camera,target_folder = self.target_folder, **camMsg.args)

                elif RELEASE_CAMERA == camMsg.cmd:
                    release_camera(camera)
                    break
            except gp.GPhoto2Error as ge2:
                print (f'got gphoto2.GPhoto2Error error {ge2}. trying to re-init')
                release_camera(camera)
                time.sleep(5) #give the situation some time to sink in...
                if INIT_CAMERA != camMsg.cmd: # this is so that we won't send INIT twice when original message was INIT.
                    self.submit_task(CameraControlMsg(INIT_CAMERA, self.capture_target))
                self.submit_task(camMsg)
            except Exception as e:
                traceback.print_exc(file=sys.stdout)
                print (f'Error in worker {self.name} loop: {e}, cmd:{camMsg}')
            finally:
                self.pq.task_done()
        print ('worker exit')
    
    def submit_task(self, msg:CameraControlMsg):
        print ('submitting task',msg)
        self.pq.put(msg)

    def empty(self):
        return self.pq.empty()


'''
    Generic Control Methods
'''

def init_camera(capture_target = DEFAULT_CAPTURE_TARGET):  
    camera = gp.Camera()
    camera.init()   
    set_capture_target(camera, capture_target)
    return camera

def release_camera(camera):
    camera.exit()

def capture_image(camera, autofocus=True):

    if autofocus:
        set_autofocus(camera, autofocus)
    print('Capturing image')
    file_path = camera.capture(gp.GP_CAPTURE_IMAGE)
    print('Camera file path: {0}/{1}'.format(file_path.folder, file_path.name))

    return file_path

def set_autofocus(camera, on=True):
    conf=camera.get_config()
    actions = conf.get_child_by_name('actions')
    actions.get_child_by_name('autofocusdrive').set_value(1 if on else 0)
    camera.set_config(conf)        

def download_image (camera, file_path, target_folder = DEFAULT_DOWNLOAD_PHOTO_FOLDER):
    print (file_path)
    if type(file_path) == gp.camera.CameraFilePath:
        file_name = file_path.folder
        file_folder = file_path.name
    else:
        file_name = os.path.basename(file_path)
        file_folder = os.path.dirname(file_path)
    print ('___join:',target_folder, file_name)
    target = os.path.join(target_folder, file_name)
    print('Copying image from',file_path,'to', target)
    camera_file = camera.file_get(file_folder, file_name, gp.GP_FILE_TYPE_NORMAL)
    print ('file copy done')
    camera_file.save(target)
    return target


def show_image (image_file, resize = 640):
    image = cv2.imread(image_file,3)
    image = imutils.resize(image, width=resize)
    cv2.imshow(image_file,image)
    
    while True:
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            exit(0)
    return 0

def preview_image():
    callback_obj = gp.check_result(gp.use_python_logging())
    camera = gp.check_result(gp.gp_camera_new())
    gp.check_result(gp.gp_camera_init(camera))
    # required configuration will depend on camera type!
    print('Checking camera config')
    # get configuration tree
    config = gp.check_result(gp.gp_camera_get_config(camera))
    # find the image format config item
    # camera dependent - 'imageformat' is 'imagequality' on some
    OK, image_format = gp.gp_widget_get_child_by_name(config, 'imageformat')
    if OK >= gp.GP_OK:
        # get current setting
        value = gp.check_result(gp.gp_widget_get_value(image_format))
        # make sure it's not raw
        if 'raw' in value.lower():
            print('Cannot preview raw images')
            # return 1
    # find the capture size class config item
    # need to set this on my Canon 350d to get preview to work at all
    OK, capture_size_class = gp.gp_widget_get_child_by_name(
        config, 'capturesizeclass')
    if OK >= gp.GP_OK:
        # set value
        value = gp.check_result(gp.gp_widget_get_choice(capture_size_class, 2))
        gp.check_result(gp.gp_widget_set_value(capture_size_class, value))
        # set config
        gp.check_result(gp.gp_camera_set_config(camera, config))
    # capture preview image (not saved to camera memory card)
    print('Capturing preview image')
    camera_file = gp.check_result(gp.gp_camera_capture_preview(camera))
    file_data = gp.check_result(gp.gp_file_get_data_and_size(camera_file))
    # display image

    data = memoryview(file_data)
    image = cv2.imdecode(np.asarray(bytearray(data), dtype=np.uint8), 3)
    cv2.imshow("preview",image)
    cv2.waitKey()
    # image.show()
    gp.check_result(gp.gp_camera_exit(camera))
    # return image


def set_capture_target(camera, value:int):
    config = gp.check_result(gp.gp_camera_get_config(camera))
    # find the capture target config item
    capture_target = gp.check_result(gp.gp_widget_get_child_by_name(config, 'capturetarget'))
    # check value in range
    count = gp.check_result(gp.gp_widget_count_choices(capture_target))
    if value < 0 or value >= count:
        print('Parameter out of range')
        return 1
    # set value
    value = gp.check_result(gp.gp_widget_get_choice(capture_target, value))
    gp.check_result(gp.gp_widget_set_value(capture_target, value))
    # set config
    gp.check_result(gp.gp_camera_set_config(camera, config))
    # clean up
    # gp.check_result(gp.gp_camera_exit(camera))
    return 0



if __name__ == "__main__":


    print (os.getpid(), "starting main")
    

    with CameraControlManagerSubProcess('worker-1') as m:
        m.submit_task(CameraControlMsg(CAPTURE_IMAGE, None))
        # m.submit_task(CAPTURE_IMAGE, None)
        # m.submit_task(CAPTURE_IMAGE, None)
        # m.submit_task(CAPTURE_IMAGE, None)

        m.pq.join()

    print ('quit main')
    