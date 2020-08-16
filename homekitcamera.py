#!/usr/bin/python3

import sys, os
# Add relative paths for the directory where the adapter is located as well as the parent
sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__),'../../base'))
from sofacollector import SofaCollector

from sofabase import sofabase, adapterbase, configbase
import devices

import math
import random
from collections import namedtuple
import requests
import json
import asyncio
import aiohttp
import logging
import pickle
import signal
import copy

import pyhap.util as util
#from pyhap.accessories.TemperatureSensor import TemperatureSensor
from pyhap.accessory import Accessory
from pyhap import camera
from pyhap.accessory_driver import AccessoryDriver

import random
import time
import datetime
import os
import functools
import concurrent.futures
import uuid
import pyqrcode
import io
#import pypng


class homekit_camera(camera.Camera):

    def __init__(self, options, adapter, device, driver, *args, **kwargs):
        self.adapter=adapter
        self.log=self.adapter.log
        self.loop=self.adapter.loop
        asyncio.set_event_loop(self.loop)
        self.device=device
        self.driver=driver
        self.imageuri=''
        self.cameraconfig={}
        self.cameraconfig['rtsp_uri']=''
        super().__init__(options, self.driver, self.device['friendlyName'])
        #self.motion=self.add_preload_service('MotionSensor')
        #self.char_detected = self.motion.configure_char('MotionDetected')
        self.set_info_service( manufacturer=device['manufacturerName'], model=device['modelName'], firmware_revision='1.0', serial_number=device['endpointId'].split(':')[2])
        
    async def initialize_camera(self):
        
        try:
            payload={ "cameraStreams": [
                        { "protocol": "RTSP", "resolution": { "width": 640, "height": 480 }, "authorizationType": "BASIC", "videoCodec": "H264", "audioCodec": "AAC"}
                    ]}
            x=await self.adapter.sendAlexaCommand("InitializeCameraStreams", "CameraStreamController", self.device['endpointId'], payload)
            self.imageuri=x['payload']['imageUri']
            for stream in x['payload']['cameraStreams']:
                if stream['protocol']=='RTSP':
                    self.rtsp_uri=stream['uri']
                    self.cameraconfig['rtsp_uri']=stream['uri']
                    self.log.info('rtsp: %s / %s' % (self.cameraconfig, self.rtsp_uri))
                    break
        except:
            self.log.error('!! Error initializing camera', exc_info=True)

    def blip(self):
        try:
            if hasattr(self, 'char_detected'):
                self.log.info('<. Blipping motion detect for %s' % self.device['friendlyName'])
                self.char_detected.set_value(True)
                time.sleep(.2)
                self.char_detected.set_value(False)
            else:
                self.log.warn('.! Could not blip motion detect for %s.  No motion detector characteristic on camera.' % self.device['friendlyName'])
        except:
            self.log.error('!! Could not blip motion detect for %s.' % self.device['friendlyName'], exc_info=True)
        
    def _detected(self):
        self.log.info('.. _detected')
        self.char_detected.set_value(True)

    def _notdetected(self):
        self.log.info('.. _notdetected')
        self.char_detected.set_value(False)

    def get_snapshot(self, image_size):  # pylint: disable=unused-argument, no-self-use
        try:   
            self.log.info('<< get snapshot: %s %s %s' % (image_size, self.imageuri, self.device))
            future=asyncio.run_coroutine_threadsafe(self.get_snap(image_size['image-width']), loop=self.loop)
            return future.result() 
        except:
            self.log.error('!! Error getting snapshot', exc_info=True)
 
    async def get_snap(self, width):

        try:
            self.log.info('.. Getting snap: %s %s' % (self.adapter.token, self.imageuri))
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector()) as client:
                # need to have token security implemented for pulling thumbnails
                async with client.get(self.imageuri) as response:
                    result=await response.read()
                    #self.log.info('Got snap: %s...' % result[:16])
                    return result  
        except:
            self.log.error('!! Error getting snap', exc_info=True)
                    
    async def start_stream(self, session_info, stream_config):
        
        try:
            self.log.info('+. Starting stream %s...', session_info['id'])
            stream_config.update(self.cameraconfig)
            if stream_config['v_max_bitrate']<1500: stream_config['v_max_bitrate']=1500
            cmd = self.start_stream_cmd.format(**stream_config).split()
            self.log.info('== Executing start stream command: "%s"', ' '.join(cmd))
            try:
                process = await asyncio.create_subprocess_exec(*cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                        limit=1024)
            except Exception as e:  # pylint: disable=broad-except
                self.log.error('!! Failed to start streaming process because of error', exc_info=True)
                return False
    
            session_info['process'] = process
    
            self.log.info('+. Started stream process - PID %d / session %s',
                            process.pid, session_info['id'])
    
            return True
        except:
            self.log.error('Error starting stream', exc_info=True)

class homekitcamera(sofabase):
    
    class adapter_config(configbase):
    
        def adapter_fields(self):
            self.ffmpeg_address=self.set_or_default('ffmpeg_address', mandatory=True)
            self.motion_map=self.set_or_default('motion_map', default={})
            
    class adapterProcess(SofaCollector.collectorAdapter):

        # Specify the audio and video configuration that your device can support
        # The HAP client will choose from these when negotiating a session.
        options = {
            "video": {
                "codec": {
                    "profiles": [
                        camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"],
                        camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["MAIN"],
                        camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["HIGH"]
                    ],
                    "levels": [
                        camera.VIDEO_CODEC_PARAM_LEVEL_TYPES['TYPE3_1'],
                        camera.VIDEO_CODEC_PARAM_LEVEL_TYPES['TYPE3_2'],
                        camera.VIDEO_CODEC_PARAM_LEVEL_TYPES['TYPE4_0'],
                    ],
                },
                "resolutions": [
                    # Width, Height, framerate
                    [320, 240, 15],  # Required for Apple Watch
                    [640, 360, 15],
                    [1024, 576, 15],
                ],
            },
            "audio": {
                "codecs": [
                    {
                        'type': 'OPUS',
                        'samplerate': 24,
                    },
                    {
                        'type': 'AAC-eld',
                        'samplerate': 16
                    }
                ],
            },
            "srtp": True,
            "address": "192.168.0.11",
            "start_stream_cmd":  (
                "ffmpeg -rtsp_transport http -re -i {rtsp_uri} "
                "-vcodec libx264 -an -pix_fmt yuv420p -r {fps} -f rawvideo -tune zerolatency -vf scale={width}x{height} "
                "-b:v {v_max_bitrate}k -bufsize {v_max_bitrate}k -payload_type 99 -ssrc {v_ssrc} -f rtp -srtp_out_suite AES_CM_128_HMAC_SHA1_80 "
                "-srtp_out_params {v_srtp_key} "
                "srtp://{address}:{v_port}?rtcpport={v_port}&localrtcpport={v_port}&pkt_size=1378"
            ),
            "test_start_stream_cmd":  (
                "ffmpeg -rtsp_transport http -re -i {rtsp_uri} "
                "-vcodec copy -an -pix_fmt yuv420p -r {fps} -f rawvideo -tune zerolatency -vf scale={width}x{height} "
                "-b:v {v_max_bitrate}k -bufsize {v_max_bitrate}k -payload_type 99 -ssrc {v_ssrc} -f rtp -srtp_out_suite AES_CM_128_HMAC_SHA1_80 "
                "-srtp_out_params {v_srtp_key} "
                "srtp://{address}:{v_port}?rtcpport={v_port}&localrtcpport={v_port}&pkt_size=1378"
            )
        }

        @property
        def collector_categories(self):
            return ['CAMERA', 'MOTION_SENSOR', 'CONTACT_SENSOR']    
    
        def __init__(self, log=None, loop=None, dataset=None, notify=None, request=None, executor=None, token=None, config=None, **kwargs):
            self.config=config
            self.drivers={}
            self.acc={}
            self.dataset=dataset
            self.log=log
            self.notify=notify
            self.polltime=5
            self.maxaid=8
            self.executor=executor
            self.portindex=0
            self.token=token
            if not loop:
                self.loop = asyncio.new_event_loop()
            else:
                self.loop=loop
            self.addExtraLogs()

        async def start(self):
            
            try:
                asyncio.get_child_watcher()
                self.log.info('..Accessory Bridge Driver started')
            except:
                self.log.error('Error during startup', exc_info=True)
                
        async def stop(self):
            try:
                self.log.info('!. Stopping Accessory Bridge Driver')
                for drv in self.drivers:
                    self.log.info('Stopping driver %s...' % drv)
                    self.drivers[drv].stop()
            except:
                self.log.error('!! Error stopping Accessory Bridge Driver', exc_info=True)

        def service_stop(self):
            try:
                self.log.info('!. Stopping Accessory Bridge Driver')
                for drv in self.drivers:
                    self.log.info('.. Stopping driver %s...' % drv)
                    self.drivers[drv].stop()
                    self.log.info('.. stopped: %s' % self.drivers[drv])
            except RuntimeError:
                pass
            except:
                self.log.error('!! Error stopping Accessory Bridge Driver', exc_info=True)

                
        def addExtraLogs(self):
            self.accessory_logger = logging.getLogger('pyhap.accessory_driver')
            self.accessory_logger.addHandler(self.log.handlers[0])
            self.accessory_logger.setLevel(logging.DEBUG)
        
            self.accessory_driver_logger = logging.getLogger('pyhap.accessory_driver')
            self.accessory_driver_logger.addHandler(self.log.handlers[0])
            self.accessory_driver_logger.setLevel(logging.DEBUG)

            self.hap_server_logger = logging.getLogger('pyhap.hap_server')
            self.hap_server_logger.addHandler(self.log.handlers[0])
            self.hap_server_logger.setLevel(logging.DEBUG)
        
            self.log.setLevel(logging.DEBUG)   
 
            
        async def virtualAddDevice(self, endpointId, obj):
            try:
                if 'CAMERA' in obj['displayCategories']:
                    await self.addAlexaCamera(obj)
            except:
                self.log.error('!! Error in virtual device add for %s' % endpointId, exc_info=True)
            

        async def addAlexaCamera(self, device, imageuri=''):
            try:
                cam=device['endpointId']
                if cam not in self.acc:
                    camid=device['endpointId'].split(':')[2]
                    camport=51836+self.portindex
                    self.drivers[cam] = AccessoryDriver(port=camport, persist_file='%s/homekitcamera-%s.json' % (self.config.cache_directory, cam))
                    self.portindex=self.portindex+1                                                                         
                    self.log.info('++ Adding Homekit Camera: %s Port: %s PIN: %s ' % (device['friendlyName'], camport, self.drivers[cam].state.pincode))
                    self.options['address']=self.config.ffmpeg_address # seems like it must be an IP address for whatever reason
                    self.acc[cam] = homekit_camera(self.options, self, device, self.drivers[cam])
                    self.drivers[cam].add_accessory(accessory=self.acc[cam])
                    #signal.signal(signal.SIGTERM, self.drivers[cam].signal_handler)
                    self.executor.submit(self.drivers[cam].start)
                    await self.acc[cam].initialize_camera()
            except:
                self.log.error('!! Error adding camera for %s' % endpointId, exc_info=True)


        async def virtualImage(self, path, client=None):
            try:
                if path.startswith('qrcode/'):
                    acc_id=path.split('/')[1]
                    if acc_id in self.acc:
                        self.log.info('.. Generating QRcode for %s %s' % (self.acc[acc_id].display_name, self.acc[acc_id].xhm_uri()))
                        qrcode = pyqrcode.create(self.acc[acc_id].xhm_uri())
                        buffer = io.BytesIO()
                        qrcode.png(buffer, scale=5, background=[0x77, 0x77, 0x77])
                        return buffer.getvalue()
            except:
                self.log.error('!! error generating qrcode for %s' % path, exc_info=True)
            return None

            
        async def virtualChangeHandler(self, deviceId, prop):
            # map open/close events to fake motion sense in order to send images through homekit notifications
            maps=self.config.motion_map
            try:
                if deviceId in maps:
                    self.acc[maps[deviceId]].blip()
            except KeyError:
                self.log.error('!! Motion simulator could not find %s in %s' % (maps[deviceId], self.acc))
            except:
                self.log.error('!! Error in virtual change handler: %s %s' % (deviceId, prop), exc_info=True)

if __name__ == '__main__':
    adapter=homekitcamera(name='homekitcamera')
    adapter.start()
