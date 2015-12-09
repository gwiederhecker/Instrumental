# -*- coding: utf-8 -*-
# Copyright 2015 Nate Bogdanowicz
"""
Driver for PCO cameras that use the PCO.camera SDK.
"""
import os.path
import atexit
from enum import Enum
from time import clock
import numpy as np
from cffi import FFI
from _pixelfly import errortext
from . import Camera
from ..util import NiceLib, check_enum, unit_mag, check_units
from .. import InstrumentTypeError, _ParamDict
from ...errors import Error, TimeoutError
from ... import Q_, u


# Notes:
# Had to add SC2_Cam.dll, sc2_cl_me4.dll (MUST BE 64-bit versions, can find these in CamView's
# folder)
# Had to fuse a bunch of header files together, manually add some typedefs, preprocess this, append
# some #defines that don't get preprocessed, save to clean.h, and open this with cffi
# It may make sense to do some simple regex-style parsing of the header files to parse out the
# #defines that we care about
# Also, I'm using the errortext module I compiled for the pixelfly library. Still unsure whether I
# should code my own version in Python so we don't require the end-user to compile it.


__all__ = ['PCO_Camera']

ffi = FFI()
with open(os.path.join(os.path.dirname(__file__), '_pco', 'clean.h')) as f:
    ffi.cdef(f.read())
ffi.cdef("""
    #define WAIT_OBJECT_0       0x00L
    #define WAIT_ABANDONED      0x80L
    #define WAIT_TIMEOUT        0x102L
    #define WAIT_FAILED         0xFFFFFFFF
    DWORD WaitForSingleObject(HANDLE hHandle, DWORD dwMilliseconds);
    BOOL ResetEvent(HANDLE hEvent);
""")
lib = ffi.dlopen('SC2_Cam.dll')
winlib = ffi.dlopen('Kernel32.dll')


def get_error_text(ret_code):
    pbuf = errortext.ffi.new('char[]', 1024)
    errortext.lib.PCO_GetErrorText(errortext.ffi.cast('unsigned int', ret_code), pbuf, len(pbuf))
    return errortext.ffi.string(pbuf)


class NicePCO(NiceLib):
    def _err_wrap(code):
        if code != 0:
            e = Error(get_error_text(code))
            e.code = code
            raise e

    def _struct_maker(*args):
        """PCO makes you fill in the wSize field of every struct"""
        struct_p = ffi.new(*args)
        struct_p[0].wSize = ffi.sizeof(struct_p[0])
        for name, field in ffi.typeof(struct_p[0]).fields:
            # Only goes one level deep for now
            if field.type.kind == 'struct':
                s = getattr(struct_p[0], name)
                s.wSize = ffi.sizeof(s)
        return struct_p

    _ffi = ffi
    _lib = lib
    _prefix = 'PCO_'

    # Special cases
    def GetTransferParameter(self):
        params_p = ffi.new('PCO_SC2_CL_TRANSFER_PARAM *')
        void_p = ffi.cast('void *', params_p)
        lib.PCO_GetTransferParameter(self._first_arg, void_p, ffi.sizeof(params_p[0]))
        # Should do error checking...
        return params_p[0]

    def SetTransferParametersAuto(self):
        lib.PCO_SetTransferParametersAuto(self._first_arg, ffi.NULL, 0)
        # Should do error checking...

    OpenCamera = ('inout', 'in', dict(first_arg=False))
    OpenCameraEx = ('inout', 'inout', dict(first_arg=False))
    CloseCamera = ('in')
    GetSizes = ('in', 'out', 'out', 'out', 'out')
    SetROI = ('in', 'in', 'in', 'in', 'in')
    GetROI = ('in', 'out', 'out', 'out', 'out')
    GetInfoString = ('in', 'in', 'buf', 'len')
    GetCameraName = ('in', 'buf40', 'len')
    GetRecordingState = ('in', 'out')
    SetRecordingState = ('in', 'in')
    SetDelayExposureTime = ('in', 'in', 'in', 'in', 'in')
    GetDelayExposureTime = ('in', 'out', 'out', 'out', 'out')
    SetFrameRate = ('in', 'out', 'in', 'inout', 'inout')
    GetFrameRate = ('in', 'out', 'out', 'out')
    ArmCamera = ('in')
    SetBinning = ('in', 'in', 'in')
    GetBinning = ('in', 'out', 'out')
    GetActiveLookupTable = ('in', 'out', 'out')
    SetActiveLookupTable = ('in', 'inout', 'inout')
    GetPixelRate = ('in', 'out')
    # GetTransferParameter = ('in', 'buf', 'len')
    GetTriggerMode = ('in', 'out')
    ForceTrigger = ('in', 'out')
    AllocateBuffer = ('in', 'inout', 'in', 'inout', 'inout')
    CamLinkSetImageParameters = ('in', 'in', 'in')
    FreeBuffer = ('in', 'in')
    CancelImages = ('in')
    AddBufferEx = ('in', 'in', 'in', 'in', 'in', 'in', 'in')
    GetBufferStatus = ('in', 'in', 'out', 'out')
    GetLookupTableInfo = ('in', 'in', 'out', 'buf20', 'len', 'out', 'out', 'out', 'out')
    GetCameraDescription = ('in', 'out')
    EnableSoftROI = ('in', 'in', 'in', 'in')


class BufferInfo(object):
    def __init__(self, num, address, event):
        self.num = num
        self.address = address
        self.event = event


class PCO_Camera(Camera):
    open_cameras = []

    def __init__(self, cam_num=0):
        self.buffers = []
        self.queue = []
        self._buf_size = 0
        self.shutter = None

        self.plib = NicePCO()
        self._open(cam_num)
        self.open_cameras.append(self)

        # Flags indicating changed data, i.e. invalid cached data
        self._sizes_changed = True
        self._transfer_param_changed = True
        self._cached_cam_desc = None

        _, _, max_width, max_height = self._get_sizes()
        self._set_ROI(0, 0, max_width, max_height)

        # For saving
        self._param_dict = _ParamDict("<PCO '{}'>".format(self.cam_num))
        self._param_dict.module = 'cameras.pco'
        self._param_dict['module'] = 'cameras.pco'
        self._param_dict['pco_cam_num'] = self.cam_num
        self._param_dict['pco_interface_type'] = self.interface_type

    # Enums
    class FrameRateMode(Enum):
        auto = 0
        framerate = 1
        exposure = 2
        strict = 3

    class TriggerMode(Enum):
        auto = 0
        software = 1
        extern_edge = 2
        extern_pulse = 3

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def set_trigger_mode(self, mode):
        """Set the trigger mode

        Parameters
        ----------
        mode : `PCO_Camera.TriggerMode` or str
            auto - Exposures occur as fast as possible
            software - Software trigger only
            extern_edge - Software trigger or external hardware trigger on a signal's edge
            extern_pulse - Hardware trigger; delay and exposure are determined by the pulse length
        """
        mode = check_enum(self.TriggerMode, mode)
        self.plib.SetTriggerMode(mode.value)

    def _open(self, cam_num):
        openStruct_p = ffi.new('PCO_OpenStruct *')
        openStruct = openStruct_p[0]
        openStruct.wSize = ffi.sizeof('PCO_OpenStruct')
        openStruct.wInterfaceType = 0xFFFF
        openStruct.wCameraNumber = cam_num
        openStruct.wCameraNumAtInterface = 0
        openStruct.wOpenFlags[0] = 0

        self.plib._first_arg = self.plib.OpenCameraEx(ffi.NULL, openStruct_p[0])[0]

        self.cam_num = openStruct.wCameraNumber
        self.interface_type = openStruct.wInterfaceType

    def close(self):
        """Close the camera"""
        self.plib.SetRecordingState(0)
        self._clear_queue()
        self._free_buffers()
        self.plib.CloseCamera()

    def _enable_soft_roi(self, enable):
        self.plib.EnableSoftROI(enable, ffi.NULL, 0)

    def _get_camera_description(self):
        if not self._cached_cam_desc:
            self._cached_cam_desc = self.plib.GetCameraDescription()
        return self._cached_cam_desc

    @unit_mag(delay='ns', exposure='ns')
    def _set_delay_exposure_time(self, delay, exposure):
        delay_ns = int(round(delay))
        exposure_ns = int(round(exposure))
        self.plib.SetDelayExposureTime(delay_ns, exposure_ns, 0, 0)

    def _get_delay_exposure_time(self):
        delay, exp, delay_timebase, exp_timebase = self.plib.GetDelayExposureTime()
        TIME_MAP = {0: 'ns', 1: 'us', 2: 'ms'}
        delay = Q_(delay, TIME_MAP[delay_timebase])
        exp = Q_(exp, TIME_MAP[exp_timebase])
        return delay, exp

    @unit_mag(framerate='mHz', exposure='ns', ret=(None, 'mHz', 'ns'))
    def _set_framerate(self, framerate, exposure='10ms', priority='auto'):
        exposure_ns = int(round(exposure))
        framerate_mHz = int(round(framerate))
        mode = check_enum(self.FrameRateMode, priority)

        self.plib.ArmCamera()
        status, framerate_mHz, exposure_ns = self.plib.SetFrameRate(mode.value, framerate_mHz,
                                                                    exposure_ns)
        return status, framerate_mHz, exposure_ns

    def _framerate(self):
        status, framerate_mHz, exposure_ns = self.plib.GetFrameRate()
        return Q_(framerate_mHz, 'mHz').to('Hz')

    def _set_ROI(self, x0, y0, x1, y1):
        desc = self._get_camera_description()
        hstep = desc.wRoiHorStepsDESC
        vstep = desc.wRoiVertStepsDESC

        if x0 % hstep != 0 or x1 % hstep != 0:
            raise Error("ROI x-coordinates must be a multiple of {}".format(hstep))
        if y0 % vstep != 0 or y1 % vstep != 0:
            raise Error("ROI y-coordinates must be a multiple of {}".format(vstep))

        # For dual ADC mode
        cx = self.max_width / 2
        if (cx - x0) != (x1 - cx):
            raise Error("ROI x-coordinates must be symmetric when in dual-ADC mode")

        # For pco.edge
        cy = self.max_height / 2
        if (cy - y0) != (y1 - cy):
            raise Error("ROI y-coordinates must be symmetric")

        try:
            self.plib.SetROI(x0+1, y0+1, x1, y1)
        except Error as e:
            if e.code == 0xA00A3001:
                raise Error("ROI coordinates out of range; given x0,y0 = {},{} and x1,y1 = {},{}"
                            " x0 must be in the range [0, width-1], and x1 must be in the range"
                            " [x0+1, width]; similarly for y0/y1".format(x0, y0, x1, y1))
            raise
        self._sizes_changed = True

    def _get_ROI(self):
        x0, y0, x1, y1 = self.plib.GetROI()
        return x0-1, y0-1, x1, y1

    def _set_centered_ROI(self, width, height):
        _, _, max_width, max_height = self._get_sizes()
        x0 = (max_width-width)/2
        y0 = (max_height-height)/2
        self._set_ROI(x0, y0, x0+width, y0+height)

    def _get_lookup_table_info(self):
        i = 0
        n_luts = 10
        info = []
        while i < n_luts:
            n_luts, desc, id, in_width, out_width, format = self.plib.GetLookupTableInfo(i)
            info.append((desc, id, in_width, out_width, format))
            i += 1
        return info

    def _data_depth(self):
        """The depth of the data format that will be transferred to the PC's buffer"""
        info = self._get_transfer_parameter()
        dataformat = info.DataFormat & lib.PCO_CL_DATAFORMAT_MASK
        depth_map = {
            lib.PCO_CL_DATAFORMAT_5x16:  16,
            lib.PCO_CL_DATAFORMAT_5x12L: 16,
            lib.PCO_CL_DATAFORMAT_5x12:  12,
            lib.PCO_CL_DATAFORMAT_5x12R: 12,
            lib.PCO_CL_DATAFORMAT_10x8:   8,
        }
        if dataformat not in depth_map:
            raise Exception("Unrecognized dataformat {}".format(dataformat))

        return depth_map[dataformat]

    def _get_pixelrate(self):
        pixelrate = self.plib.GetPixelRate()
        return Q_(pixelrate, 'Hz').to('MHz')

    def _get_transfer_parameter(self):
        if self._transfer_param_changed:
            self._cached_transfer_param = self.plib.GetTransferParameter()
            self._transfer_param_changed = False
        return self._cached_transfer_param

    def _get_sizes(self):
        if self._sizes_changed:
            x_act, y_act, x_max, y_max = self.plib.GetSizes()
            self._cached_sizes = x_act, y_act, x_max, y_max
            self._sizes_changed = False
        return self._cached_sizes

    def _find_good_data_format(self, image_width):
        if self._get_pixelrate() < Q_(96, 'MHz'):
            format = lib.PCO_CL_DATAFORMAT_5x16
        else:
            if image_width <= 1920:
                format = lib.PCO_CL_DATAFORMAT_5x16
            else:
                format = lib.PCO_CL_DATAFORMAT_5x12
        return format

    def _allocate_buffers(self, nbufs=None):
        if nbufs is None:
            if len(self.buffers) > 1:
                nbufs = len(self.buffers)
            elif self.shutter == 'continuous':
                nbufs = 2
            else:
                nbufs = 1

        # Clean up existing buffers
        self.plib.SetRecordingState(0)
        self._clear_queue()
        self._free_buffers()

        self._buf_size = self._frame_size()

        # Allocate new buffers
        for i in range(nbufs):
            bufnum, buf_p, event = self.plib.AllocateBuffer(-1, self._buf_size, ffi.NULL, ffi.NULL)
            self.buffers.append(BufferInfo(bufnum, buf_p, event))

    def _free_buffers(self):
        for buf in self.buffers:
            self.plib.FreeBuffer(buf.num)
        self.buffers = []

    def _clear_queue(self):
        self.queue = []
        self.plib.CancelImages()

    def _push_on_queue(self, buf):
        width, height, _, _ = self._get_sizes()
        depth = self._data_depth()
        self.plib.AddBufferEx(0, 0, buf.num, width, height, depth)
        self.queue.append(buf)

    def _frame_size(self):
        """Calculate the size (in bytes) a buffer needs to hold an image with the current
        settings."""
        width, height, _, _ = self._get_sizes()
        return (width * height * self._data_depth()) / 16 * 2

    def _set_binning(self, hbin, vbin):
        self.plib.SetBinning(hbin, vbin)

    def start_capture(self, **kwds):
        self._handle_kwds(kwds)

        self._set_binning(kwds['vbin'], kwds['hbin'])
        self._set_ROI(kwds['left'], kwds['top'], kwds['right'], kwds['bot'])
        self._set_delay_exposure_time(delay='0s', exposure=kwds['exposure_time'])
        self.plib.ArmCamera()
        self._allocate_buffers(kwds['n_frames'])
        self.plib.ArmCamera()

        # Prepare CameraLink interface
        self.plib.SetTransferParametersAuto()
        self.plib.ArmCamera()
        self.plib.CamLinkSetImageParameters(self.width, self.height)

        # Add buffers to the queue
        for buf in self.buffers:
            self._push_on_queue(buf)

        self.plib.SetRecordingState(1)
        self.plib.ForceTrigger()

    @check_units(timeout='ms')
    def get_captured_image(self, timeout='1s', copy=True):
        """get_captured_image(timeout='1s', copy=True)"""
        width, height, _, _ = self._get_sizes()
        frame_size = self._frame_size()
        image_arrs = []
        try:
            start_time = clock() * u.s
            # Can't loop directly through queue since wait_for_frame modifies it
            while self.queue:
                buf = self.queue[0]
                elapsed_time = clock() * u.s - start_time
                frame_ready = self.wait_for_frame(timeout - elapsed_time)

                if not frame_ready:
                    raise TimeoutError

                if copy:
                    image_buf = buffer(ffi.buffer(buf.address, frame_size)[:])
                else:
                    image_buf = buffer(ffi.buffer(buf.address, frame_size))

                # Convert to array (currently assumes mono16)
                array = np.frombuffer(image_buf, np.uint16)
                array = array.reshape((height, width))
                image_arrs.append(array)
        finally:
            # Stop recording and clean up queue
            self.plib.SetRecordingState(0)
            self._clear_queue()

        if len(image_arrs) == 1:
            return image_arrs[0]
        else:
            return tuple(image_arrs)

    def grab_image(self, timeout='1s', copy=True, **kwds):
        self.start_capture(**kwds)
        return self.get_captured_image(timeout=timeout, copy=copy)

    @check_units(framerate='Hz')
    def start_live_video(self, framerate='10Hz', **kwds):
        """start_live_video(self, framerate='10Hz', **kwds)"""
        self._handle_kwds(kwds)

        self._set_binning(kwds['vbin'], kwds['hbin'])
        self._set_ROI(kwds['left'], kwds['top'], kwds['right'], kwds['bot'])
        self.plib.ArmCamera()

        # Prepare CameraLink interface
        width, height, _, _ = self._get_sizes()
        self.plib.SetTransferParametersAuto()
        self._set_framerate(framerate, kwds['exposure_time'])
        self.plib.ArmCamera()
        self.plib.CamLinkSetImageParameters(width, height)

        self.shutter = 'continuous'
        if self._frame_size() != self._buf_size or len(self.buffers) < 2:
            self._allocate_buffers(nbufs=2)
        self.plib.ArmCamera()

        # Add all the buffers to the queue
        for buf in self.buffers:
            self._push_on_queue(buf)

        self.plib.SetRecordingState(1)
        self.plib.ForceTrigger()

    def stop_live_video(self):
        self.plib.SetRecordingState(0)
        self._clear_queue()
        self._free_buffers()
        self.shutter = None

    @unit_mag(timeout='ms')
    def wait_for_frame(self, timeout='1s'):
        """wait_for_frame(self, timeout='1s')"""
        if not self.queue:
            raise Exception("No queued buffers!")

        timeout = max(0, timeout)  # Negative timeout is equivalent to 0

        # Wait for the next buffer event to fire
        buf = self.queue[0]
        ret = winlib.WaitForSingleObject(buf.event, int(timeout))
        if ret == winlib.WAIT_OBJECT_0:
            dll_status, drv_status = self.plib.GetBufferStatus(buf.num)
            if drv_status != 0:
                raise Exception(get_error_text(drv_status))
            winlib.ResetEvent(buf.event)
        elif ret == winlib.WAIT_TIMEOUT:
            return False
        else:
            raise Error("Failed to grab image")

        self.last_buffer = self.queue.pop(0)  # Pop and save only on success

        if self.shutter == 'continuous':
            self._push_on_queue(buf)  # Add buf back to the end of the queue

        return True

    def latest_frame(self, copy=True):
        buf_info = self.last_buffer
        if copy:
            buf = buffer(ffi.buffer(buf_info.address, self._frame_size())[:])
        else:
            buf = buffer(ffi.buffer(buf_info.address, self._frame_size()))

        width, height, _, _ = self._get_sizes()
        array = np.frombuffer(buf, np.uint16)
        array = array.reshape((height, width))
        return array

    def _color_mode(self):
        desc = self._get_camera_description()
        if desc.wPatternTypeDESC & 0x01:  # All odd-numbered sensors are color
            return 'RGB32'
        else:
            return 'mono' + str(self.bit_depth)

    def _width(self):
        width, _, _, _ = self._get_sizes()
        return width

    def _height(self):
        _, height, _, _ = self._get_sizes()
        return height

    width = property(lambda self: self._width())
    height = property(lambda self: self._height())
    max_width = property(lambda self: self._get_sizes()[2])
    max_height = property(lambda self: self._get_sizes()[3])

    #: Color mode string ('mono16', 'RGB32', etc.)
    color_mode = property(lambda self: self._color_mode())

    #: Number of bits per pixel in the on-PC image
    bit_depth = property(lambda self: self._data_depth())

    #: Framerate in live mode
    framerate = property(lambda self: self._framerate())


def list_instruments():
    plib = NicePCO()

    openStruct_p = ffi.new('PCO_OpenStruct *')
    openStruct = openStruct_p[0]
    openStruct.wSize = ffi.sizeof('PCO_OpenStruct')
    openStruct.wCameraNumber = 0

    cameras = []
    prev_handle = None

    while True:
        openStruct.wInterfaceType = 0xFFFF  # Try all interfaces
        openStruct.wCameraNumAtInterface = 0
        openStruct.wOpenFlags[0] = 0

        hCam, _ = plib.OpenCameraEx(ffi.NULL, openStruct_p)  # This is reallllyyyy sloowwwww

        if openStruct.wInterfaceType == 0xFFFF or hCam == prev_handle:
            # OpenCameraEx doesn't seem to return error upon not finding a camera, so if it didn't
            # set wInterfaceType, or the handle is the same as the previous handle, we assume it
            # found no camera
            plib.CloseCamera(hCam)
            break
        else:
            param_dict = _ParamDict("<PCO '{}'>".format(openStruct.wCameraNumber))
            param_dict.module = 'cameras.pco'
            param_dict['module'] = 'cameras.pco'
            param_dict['pco_cam_num'] = openStruct.wCameraNumber
            param_dict['pco_interface_type'] = openStruct.wInterfaceType

            cameras.append(param_dict)
            plib.CloseCamera(hCam)
            prev_handle = hCam

        openStruct.wCameraNumber += 1

    return cameras


def _instrument(params):
    if 'pco_cam_num' in params:
        cam = PCO_Camera(params['pco_cam_num'])
    elif params.module == 'cameras.pco':
        cam = PCO_Camera()
    else:
        raise InstrumentTypeError()
    return cam


@atexit.register
def _cleanup():
    for cam in PCO_Camera.open_cameras:
        try:
            cam.close()
        except:
            pass