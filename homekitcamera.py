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
import concurrent.futures

import random
import time
import datetime
import os
import functools
import concurrent.futures
import uuid
import pyqrcode
import io
import ffmpeg
#import pypng

import socket


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
        self.active_streams=[]
        self.monitored_streams=[]
        super().__init__(options, self.driver, self.device['friendlyName'])
        #self.motion=self.add_preload_service('MotionSensor')
        #self.char_detected = self.motion.configure_char('MotionDetected')
        self.set_info_service( manufacturer=device['manufacturerName'], model=device['modelName'], firmware_revision='1.0', serial_number=device['endpointId'].split(':')[2])

        
    async def initialize_camera(self):
        
        try:
            payload = { "cameraStreams": [] }
            camera_result = await self.adapter.sendAlexaCommand("InitializeCameraStreams", "CameraStreamController", self.device['endpointId'], payload)
            self.imageuri = camera_result['payload']['imageUri']
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
            #self.log.info('<< get snapshot: %s %s %s' % (image_size, self.imageuri, self.device))
            #self.log.info('<< get snapshot: %s %s' % (image_size, self.imageuri))
            future=asyncio.run_coroutine_threadsafe(self.get_snap(image_size['image-width']), loop=self.loop)
            return future.result() 
        except:
            self.log.error('!! Error getting snapshot', exc_info=True)
 
 
 
    def swap_host(self,url):
        # TODO CHEESE - This is a hack to test exchanging the ui URL with the gateway URL
        # This should probably be moved to the actual UI adapter and allow it to mangle the 
        # URL appropriately since the more "real" URL would be the gateway
        return url.replace("https://home.dayton.home", "https://home.dayton.home:6443")
 
    async def get_snap(self, width):

        try:
            image_uri=self.swap_host(self.imageuri)
            if width:
                image_uri+="?width=%s" % width

            if self.adapter.dataset.token==None:
                self.log.error('!! Error - no token %s' % self.adapter.dataset.token)
                return ""
                
            headers={ "Content-type": "text/xml", "authorization": self.adapter.dataset.token }
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(),timeout=timeout) as client:
                # need to have token security implemented for pulling thumbnails
                async with client.get(image_uri, headers=headers) as response:
                    result=await response.read()
                    #self.log.info('<< snapshot: %s %s...' % (image_uri, result[:16]))
                    return result  
        except concurrent.futures._base.TimeoutError:
            self.log.error('!! Error getting snapshot for %s (timeout) - %s' % (self.device['endpointId'], self.imageuri))

        except:
            self.log.error('!! Error getting snapshot for %s (timeout) - %s' % (self.device['endpointId'], self.imageuri), exc_info=True)

    def launch_ffmpeg(self, rtsp_alias):
        try:
            record_process = (
                ffmpeg
                    .input('rtsp://%s:%s/%s' % (self.config.nvr_address, self.config.rtsp_port, rtsp_alias), rtsp_transport="tcp")
                    .output("%s/unifivideo/%s/stream.m3u8" % (self.config.video_directory, self.deviceid), format="hls", vsync=0, vcodec="copy", acodec="copy", 
                            hls_flags="delete_segments+append_list",
                            hls_base_url="/video/unifivideo/%s/" % self.deviceid, 
                            hls_segment_filename="%s/unifivideo/%s/cctv-%%d.ts" % (self.config.video_directory, self.deviceid),
                            hls_time=2,
                            hls_list_size=5
                            )
                    .global_args('-hide_banner', '-nostats', '-loglevel', 'panic')
                    .overwrite_output()
            
                .run_async(pipe_stdout=True, pipe_stderr=True)
            )
            self.log.info('.. ffmpeg started for %s (%s)' % (self.device.friendlyName, self.deviceid))
            return record_process
        except:
            self.log.info('!! Error starting ffmpeg for streaming camera %s' % self.deviceid, exc_info=True)
        return None               


    async def stop_stream(self, session_info): 
        
        # This replaces the built-in stop_stream to avoid conflicts with the communicate stage
        # and the async logging 
        
        try:
            session_id = session_info['id']
            self.active_streams.remove(session_id)
            self.log.info('>| stream %s stopping ffmpeg process' % session_id)
            while session_id in self.monitored_streams:
                await asyncio.sleep(.1)
            self.log.info('-|  stream %s stopped' % session_id)
        except:
            self.log.error('!! Error in stop_stream', exc_info=True)

                        
    async def start_stream(self, session_info, stream_config):
        
        try:
            self.log.info('stream_config: %s' % stream_config)
            self.log.info('session_info %s' % session_info)
            if 'process' in session_info:
                self.log.info(".. stream %s ffmpeg process already started" % session_info['id'])
                return True
            payload={ "cameraStreams": [
                        { "protocol": "RTSP", "resolution": { "width": stream_config['width'], "height": stream_config['height'] }, 
                                "authorizationType": "NONE", "videoCodec": "H264", "audioCodec": "AAC"}
                    ]}

            found_stream=False
            lowest_res=None
            for cap in self.device['capabilities']:
                if cap['interface']=="Alexa.CameraStreamController":
                    for stream in cap["cameraStreamConfigurations"]:
                        if 'RTSP' in stream['protocols']:
                            for res in stream['resolutions']:
                                if lowest_res==None or (res['width']>=stream_config['width'] and lowest_res['width']>res['width']):
                                    lowest_res=res
                                    if res['width']==stream_config['width']:
                                        break
                                    
            if lowest_res:
                payload={ "cameraStreams": [
                            { "protocol": "RTSP", "resolution": res, "authorizationType": "NONE", "videoCodec": "H264", "audioCodec": "AAC"}
                        ]}
                camera_result = await self.adapter.sendAlexaCommand("InitializeCameraStreams", "CameraStreamController", self.device['endpointId'], payload)

                lowest_best=None
                for stream in camera_result['payload']['cameraStreams']:
                    if stream['protocol']=='RTSP' and stream['resolution']['width']>=stream_config['width'] and (lowest_best==None or lowest_best>stream['resolution']['width']):
                        stream_config['rtsp_uri']=stream['uri']
                        found_stream=True
                
            if not found_stream:
                self.log.error('!! error - could not get appropriate stream from: %s' % camera_result)
                return false

            self.log.info('+. stream %s starting: %sx%s %s' % (session_info['id'], stream_config['width'], stream_config['height'], stream_config['rtsp_uri']))
            #stream_config.update(self.cameraconfig)
            #if stream_config['v_max_bitrate']<1500: stream_config['v_max_bitrate']=1500
            #if stream_config['fps']>15: stream_config['fps']=15
            stream_config['v_buffer_size']=stream_config['v_max_bitrate']*2
            stream_config['a_buffer_size']=stream_config['a_max_bitrate']*2
            stream_config['v_mtu']= 188 * 3
            stream_config['a_mtu']= 188 * 1
            
            # This is coming across as a single
            stream_config['v_payload_type']=ord(stream_config['v_payload_type'])
            stream_config['a_payload_type']=ord(stream_config['a_payload_type'])

            self.log.info('~~ Ports to use - v_port: %s a_port: %s' % 
                                (stream_config['v_port'], stream_config['a_port']))
            
            cmd = self.start_stream_cmd.format(**stream_config).split()
            self.log.info('== stream %s launching: "%s"' %  (session_info['id'], ' '.join(cmd)))
            try:
                process = await asyncio.create_subprocess_exec(*cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                limit=1024)
            except Exception as e:  # pylint: disable=broad-except
                self.log.error('!! stream %s failed to start streaming' % session_info['id'], exc_info=True)
                return False
                
            session_info['process'] = process
            self.log.info('+= stream %s started with PID %d' % (session_info['id'], process.pid))

            # TODO/CHEESE 10/5/20 - Can't use this logging process due to a conflict with the way stop_stream manages the FFMPEG shutdown
            # When communicate is called, it crashes because readline is still running
            # Investigate for better FFMPEG monitoring in the future
            self.active_streams.append(session_info['id'])
            self.monitored_streams.append(session_info['id'])
            await self.monitor_process(process, session_info)

            return True
        except:
            self.log.error('!! Error starting stream', exc_info=True)
        return False

    def log_monitor(self, data):
        line=data.decode().strip('\n').strip('\r')
        if len(line)>0 and line[1]!="=":
            self.log.info('~~ %s' % line)


    # streaming test for results from ffmpeg
    async def monitor_process(self, process, session_info):  
        try:
            session_id = session_info['id']
            await asyncio.wait([
                self._read_stream(process.stdout, self.log_monitor, session_info),
                self._read_stream(process.stderr, self.log_monitor, session_info)
            ])
            
            self.log.error('!! Done with stream %s. Attempting to stop' % session_id)
            
            try:
                process.terminate()
                _, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=2.0)
                self.log.debug('.. Stream command stderr: %s', stderr)
            except asyncio.TimeoutError:
                self.log.error('!! Timeout stopping stream %s. Attempting to kill' % session_id)
                process.kill()
 
            result=await process.wait()
            self.log.info('result: %s' % result)
            self.monitored_streams.remove(session_id)
            return result
        except:
            self.log.error('Error monitoring ffmpeg process', exc_info=True)
            self.monitored_streams.remove(session_id)
        return False


    async def _read_stream(self, stream, cb, session_info):  
        try:
            session_id = session_info['id']
            while session_id in self.active_streams:
                #self.log.info('rsid: %s / %s' % (session_id, self.active_streams))
                try:
                    line = await asyncio.wait_for(stream.readline(), 1)
                    #line = await stream.readline()
                    if line:
                        cb(line)
                    else:
                        break
                except asyncio.TimeoutError:
                    pass
                except:
                    self.log.error('Error', exc_info=True)
            #self.log.info('XXXX done reading streaam for %s' % session_id)
        except:
            self.log.error('Error in readstream', exc_info=True)


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
                    [1920, 1080, 30],
                    [1280, 960, 30],
                    [1280, 720, 30],
                    [1024, 768, 30],
                    [640, 480, 30],
                    [640, 360, 30],
                    [480, 360, 30],
                    [480, 270, 30],
                    [320, 240, 30],
                    [320, 240, 15], 
                    [320, 180, 30]
                ],
            },
            "audio": {
                "codecs": [
                    {
                        'type': 'AAC-eld',
                        'samplerate': 16
                    }
                ],
            },
            "srtp": True,
            "address": "192.168.0.11",
            "start_stream_cmd":  (
                "ffmpeg -fflags +genpts -use_wallclock_as_timestamps 1 -loglevel panic -nostats -hide_banner -rtsp_transport tcp -i {rtsp_uri} "
                
                "-map 0:v -vcodec copy -f rawvideo -pix_fmt yuvj420p -r {fps} "
                "-b:v {v_max_bitrate}k -bufsize {v_buffer_size}k -maxrate {v_max_bitrate}k "
                "-payload_type {v_payload_type} -ssrc {v_ssrc} "
                "-f rtp -srtp_out_suite AES_CM_128_HMAC_SHA1_80 -r {fps} "
                "-srtp_out_params {v_srtp_key} "
                "srtp://{address}:{v_port}?rtcpport={v_port}&localrtcpport={v_port}&pkt_size={v_mtu} "
                
                "-map 0:a -acodec libfdk_aac -profile:a aac_eld -flags +global_header -f null -ar {a_sample_rate}k "
                "-b:a {a_max_bitrate}k -bufsize {a_buffer_size}k -ac 1 "
                "-payload_type {a_payload_type} -ssrc {a_ssrc} -f rtp -srtp_out_suite AES_CM_128_HMAC_SHA1_80 "
                "-srtp_out_params {a_srtp_key} "
                "srtp://{address}:{a_port}?rtcpport={a_port}&localrtcpport={a_port}&pkt_size={a_mtu} "
            ),
        }
        # specific features were required to avoid a strange error with Non-monotonous time errors
        # which were reporting an initial timestamp 30000 higher than it should have been. 
        # this would happen about 50% of the time.
        # while this error was not totally figured out, they resolve the problem for now:
        # -fflags +genpts -use_wallclock_as_timestamps 1


        @property
        def collector_categories(self):
            return ['CAMERA', 'MOTION_SENSOR', 'CONTACT_SENSOR']    
    
        def __init__(self, log=None, loop=None, dataset=None, notify=None, request=None, executor=None, token=None, config=None, **kwargs):
            super().__init__(log=log, loop=loop, dataset=dataset, config=config)
            self.drivers={}
            self.acc={}
            self.notify=notify
            self.polltime=5
            self.maxaid=8
            self.executor=executor
            self.portindex=0
            self.token=token
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

        class eight_filter(logging.Filter):
            def filter(self, record):
                record.filename_eight = record.filename.split(".")[0][:8]
                return True   
                
        def addExtraLogs(self):
            self.accessory_logger = logging.getLogger('pyhap.accessory_driver')
            self.accessory_logger.addFilter(self.eight_filter()) 
            self.accessory_logger.addHandler(self.log.handlers[0])
            self.accessory_logger.setLevel(logging.DEBUG)
        
            self.accessory_driver_logger = logging.getLogger('pyhap.accessory_driver')
            self.accessory_driver_logger.addFilter(self.eight_filter()) 
            self.accessory_driver_logger.addHandler(self.log.handlers[0])
            self.accessory_driver_logger.setLevel(logging.DEBUG)

            self.hap_server_logger = logging.getLogger('pyhap.hap_server')
            self.hap_server_logger.addFilter(self.eight_filter()) 
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


        async def virtualImage(self, path, client=None, width=None, height=None):
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
