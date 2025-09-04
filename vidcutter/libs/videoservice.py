#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#######################################################################
#
# VidCutter - media cutter & joiner
#
# copyright © 2018 Pete Alexandrou
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#######################################################################

import errno
import logging
import os
import re
import shlex
import sys
from bisect import bisect_left
from functools import partial
from typing import List, Optional, Union

from PyQt5.QtCore import (pyqtSignal, pyqtSlot, QDir, QFileInfo, QObject, QProcess, QProcessEnvironment, QSettings,
                          QSize, QStandardPaths, QStorageInfo, QTemporaryFile, QTime)
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtWidgets import QMessageBox, QWidget

from vidcutter.libs.config import Config, InvalidMediaException, Streams, ToolNotFoundException
from vidcutter.libs.ffmetadata import FFMetadata
from vidcutter.libs.munch import Munch
from vidcutter.libs.widgets import VCMessageBox

try:
    # noinspection PyPackageRequirements
    from simplejson import loads, JSONDecodeError
except ImportError:
    from json import loads, JSONDecodeError


class VideoService(QObject):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)
    addScenes = pyqtSignal(list)

    frozen = getattr(sys, 'frozen', False)
    spaceWarningThreshold = 200
    spaceWarningDelivered = False
    smartcutError = False

    config = Config()

    def __init__(self, settings: QSettings, parent: QWidget):
        super(VideoService, self).__init__(parent)
        self.settings = settings
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        try:
            self.backends = VideoService.findBackends(self.settings)
            self.proc = VideoService.initProc()
            if hasattr(self.proc, 'errorOccurred'):
                self.proc.errorOccurred.connect(self.cmdError)
            self.lastError = ''
            self.media, self.source = None, None
            self.chapter_metadata = None
            self.keyframes = []
            self.streams = Munch()
            self.mappings = []
        except ToolNotFoundException as e:
            self.logger.exception(e.msg, exc_info=True)
            QMessageBox.critical(getattr(self, 'parent', None), 'Missing libraries', e.msg)

    def setMedia(self, source: str) -> None:
        try:
            self.source = QDir.toNativeSeparators(source)
            self.media = self.probe(source)
            if self.media is not None:
                if getattr(self.parent, 'verboseLogs', False):
                    self.logger.info(self.media)
                for codec_type in Streams.__members__:
                    setattr(self.streams, codec_type.lower(),
                            [stream for stream in self.media.streams if stream.codec_type == codec_type.lower()])
                if len(self.streams.video):
                    self.streams.video = self.streams.video[0]  # we always assume one video stream per media file
                else:
                    raise InvalidMediaException('Could not load video stream for {}'.format(source))
                self.mappings.clear()
                # noinspection PyUnusedLocal
                [self.mappings.append(True) for i in range(int(self.media.format.nb_streams))]
        except OSError as e:
            if e.errno == errno.ENOENT:
                errormsg = '{0}: {1}'.format(os.strerror(errno.ENOENT), source)
                self.logger.error(errormsg)
                raise FileNotFoundError(errormsg)

    @staticmethod
    def findBackends(settings: QSettings) -> Munch:
        tools = Munch(ffmpeg=None, ffprobe=None, mediainfo=None)
        settings.beginGroup('tools')
        tools.ffmpeg = settings.value('ffmpeg', None, type=str)
        tools.ffprobe = settings.value('ffprobe', None, type=str)
        tools.mediainfo = settings.value('mediainfo', None, type=str)
        for tool in list(tools.keys()):
            path = tools[tool]
            if path is None or not len(path) or not os.path.isfile(path):
                for exe in VideoService.config.binaries[os.name][tool]:
                    if VideoService.frozen:
                        binpath = VideoService.getAppPath(os.path.join('bin', exe))
                    else:
                        binpath = QStandardPaths.findExecutable(exe)
                        if not len(binpath):
                            binpath = QStandardPaths.findExecutable(exe, [VideoService.getAppPath('bin')])
                    if os.path.isfile(binpath) and os.access(binpath, os.X_OK):
                        tools[tool] = binpath
                        if not VideoService.frozen:
                            settings.setValue(tool, binpath)
                        break
        settings.endGroup()
        if tools.ffmpeg is None:
            raise ToolNotFoundException('FFmpeg missing')
        if tools.ffprobe is None:
            raise ToolNotFoundException('FFprobe missing')
        if tools.mediainfo is None:
            raise ToolNotFoundException('MediaInfo missing')
        return tools

    @staticmethod
    def initProc(program: str=None, finish: pyqtSlot=None, workingdir: str=None) -> QProcess:
        p = QProcess()
        p.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        p.setProcessChannelMode(QProcess.MergedChannels)
        if workingdir is not None:
            p.setWorkingDirectory(workingdir)
        if program is not None:
            p.setProgram(program)
        if finish is not None:
            p.finished.connect(finish)
        return p

    def checkDiskSpace(self, path: str) -> None:
        # noinspection PyCallByClass
        if self.spaceWarningDelivered or not QFileInfo.exists(path):
            return
        info = QStorageInfo(path)
        available = info.bytesAvailable() / 1000 / 1000
        if available < VideoService.spaceWarningThreshold:
            warnmsg = 'There is less than {}MB of free disk space in the '.format(VideoService.spaceWarningThreshold)
            warnmsg += 'folder selected to save your media. '
            warnmsg += 'VidCutter will fail if space runs out before processing completes.'
            spacewarn = VCMessageBox('Warning', 'Disk space alert', warnmsg, self.parentWidget())
            spacewarn.addButton(VCMessageBox.Ok)
            spacewarn.exec_()
            self.spaceWarningDelivered = True

    @staticmethod
    def captureFrame(settings: QSettings, source: str, frametime: str, thumbsize: QSize=None,
                     external: bool=False) -> QPixmap:
        if thumbsize is None:
            thumbsize = VideoService.config.thumbnails['INDEX']
        capres = QPixmap()
        img = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX.jpg'))
        if img.open():
            imagecap = img.fileName()
            cmd = VideoService.findBackends(settings).ffmpeg
            tsize = '{0:d}x{1:d}'.format(thumbsize.width(), thumbsize.height())
            args = '-hide_banner -ss {frametime} -i "{source}" -vframes 1 -s {tsize} -y "{imagecap}"'.format(**locals())
            proc = VideoService.initProc()
            if proc.state() == QProcess.NotRunning:
                proc.start(cmd, shlex.split(args))
                proc.waitForFinished(-1)
                if proc.exitStatus() == QProcess.NormalExit and proc.exitCode() == 0:
                    capres = QPixmap(imagecap, 'JPG')
                if external:
                    painter = QPainter(capres)
                    painter.drawPixmap(0, 0, QPixmap(':/images/external.png', 'PNG'))
                    painter.end()
        img.remove()
        return capres

    # noinspection PyBroadException
    def testJoin(self, file1: str, file2: str) -> bool:
        result = False
        self.logger.info('attempting to test joining of "{0}" & "{1}"'.format(file1, file2))
        try:
            # 1. check audio + video codecs
            file1_codecs = self.codecs(file1)
            file2_codecs = self.codecs(file2)
            if file1_codecs != file2_codecs:
                self.logger.info('join test failed for {0} and {1}: codecs mismatched'.format(file1, file2))
                self.lastError = '<p>The audio + video format of this media file is not the same as the files ' \
                                 'already in your clip index.</p>' \
                                 '<div align="center">Current files are <b>{0}</b> (video) and ' \
                                 '<b>{1}</b> (audio)<br/>' \
                                 'Failed media is <b>{2}</b> (video) and <b>{3}</b> (audio)</div>'
                self.lastError = self.lastError.format(file1_codecs[0], file1_codecs[1],
                                                       file2_codecs[0], file2_codecs[1])
                return result
            # 2. check frame sizes
            size1 = self.framesize(file1)
            size2 = self.framesize(file2)
            if size1 != size2:
                self.logger.info('join test failed for {0} and {1}: frame size mismatched'.format(file1, file2))
                self.lastError = '<p>The frame size of this media file is not the same as the files already in ' \
                                 'your clip index.</p>' \
                                 '<div align="center">Current media clips are <b>{0}x{1}</b>' \
                                 '<br/>Failed media file is <b>{2}x{3}</b></div>'
                self.lastError = self.lastError.format(size1.width(), size1.height(), size2.width(), size2.height())
                return result
            # 2. generate temporary file handles
            _, ext = os.path.splitext(file1)
            file1_cut = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX{}'.format(ext)))
            file2_cut = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX{}'.format(ext)))
            final_join = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX{}'.format(ext)))
            # 3. produce 4 secs clips from input files for join test
            if file1_cut.open() and file2_cut.open() and final_join.open():
                result1 = self.cut(file1, file1_cut.fileName(), '00:00:00.000', '00:00:04.00', False)
                result2 = self.cut(file2, file2_cut.fileName(), '00:00:00.000', '00:00:04.00', False)
                if result1 and result2:
                    # 4. attempt join of temp 2 second clips
                    result = self.join([file1_cut.fileName(), file2_cut.fileName()],
                                       final_join.fileName(), False, None)
            VideoService.cleanup([file1_cut.fileName(), file2_cut.fileName(), final_join.fileName()])
        except BaseException:
            self.logger.exception('Exception in VideoService.testJoin', exc_info=True)
            result = False
        return result

    def framesize(self, source: str = None) -> QSize:
        if source is None and hasattr(self.streams, 'video'):
            return QSize(int(self.streams.video.width), int(self.streams.video.height))
        else:
            args = '-i "{}"'.format(source)
            result = self.cmdExec(self.backends.ffmpeg, args, True)
            matches = re.search(r'Stream.*Video:.*[,\s](?P<width>\d+?)x(?P<height>\d+?)[,\s]',
                                result, re.DOTALL).groupdict()
            return QSize(int(matches['width']), int(matches['height']))

    def duration(self, source: str = None) -> QTime:
        if source is None and hasattr(self.media, 'format') and self.parent is not None:
            return self.parent.delta2QTime(float(self.media.format.duration))
        else:
            args = '-i "{}"'.format(source)
            result = self.cmdExec(self.backends.ffmpeg, args, True)
            matches = re.search(r'Duration:\s(?P<hrs>\d+?):(?P<mins>\d+?):(?P<secs>\d+\.\d+?),',
                                result, re.DOTALL).groupdict()
            secs, msecs = matches['secs'].split('.')
            return QTime(int(matches['hrs']), int(matches['mins']), int(secs), int(msecs))

    def codecs(self, source: str = None) -> tuple:
        if source is None and hasattr(self.streams, 'video'):
            return self.streams.video.codec_name, self.streams.audio[0].codec_name if len(self.streams.audio) else None
        else:
            args = '-i "{}"'.format(source)
            result = self.cmdExec(self.backends.ffmpeg, args, True)
            vcodec_match = re.search(r'Stream.*Video:\s(\w+)', result)
            vcodec = vcodec_match.group(1) if vcodec_match else None
            acodec_match = re.search(r'Stream.*Audio:\s(\w+)', result)
            acodec = acodec_match.group(1) if acodec_match else None
            return vcodec, acodec

    def parseMappings(self, allstreams: bool = True) -> str:
        if not len(self.mappings) or (self.parent is not None and self.parent.hasExternals()):
            # SUBTITLE FIX: Use essential streams to avoid subtitle codec issues
            if allstreams:
                return '-map 0:v -map 0:a '
            else:
                return ''
        # if False not in self.mappings:
        #     return '-map 0 '
        output = ''
        for stream_id in range(len(self.mappings)):
            if self.mappings[stream_id]:
                # SUBTITLE FIX: Skip subtitle/data streams to avoid codec issues
                if hasattr(self, 'streams') and hasattr(self.media, 'streams') and stream_id < len(self.media.streams):
                    stream = self.media.streams[stream_id]
                    if hasattr(stream, 'codec_type') and stream.codec_type in ['subtitle', 'data']:
                        self.logger.info('[DEBUG] parseMappings: Skipping problematic {} stream {}'.format(stream.codec_type, stream_id))
                        continue
                output += '-map 0:{} '.format(stream_id)
        
        # If allstreams=False but no mappings were applied, ensure we at least include video and audio
        if not allstreams and not output and hasattr(self, 'streams'):
            # Always include the first video stream
            if hasattr(self.streams, 'video') and self.streams.video:
                output += '-map 0:v:0 '
            # Always include the first audio stream if it exists
            if hasattr(self.streams, 'audio') and len(self.streams.audio) > 0:
                output += '-map 0:a:0 '
                
        return output

    def finalize(self, source: str) -> bool:
        self.checkDiskSpace(source)
        source_file, source_ext = os.path.splitext(source)
        final_filename = '{0}_FINAL{1}'.format(source_file, source_ext)
        args = '-v error -i "{}" -map 0 -c copy -y "{}"'.format(source, final_filename)
        result = self.cmdExec(self.backends.ffmpeg, args)
        if result and os.path.exists(final_filename):
            os.replace(final_filename, source)
            return True
        else:
            # Log error details for debugging
            if os.path.exists(final_filename):
                # Get stderr output for error details
                error_args = '-v error -i "{}" -map 0 -c copy -y "{}"'.format(source, final_filename)
                error_output = self.cmdExec(self.backends.ffmpeg, error_args, output=True)
                self.logger.error('FFmpeg finalize failed. Command: {} {}. Error: {}'.format(
                    self.backends.ffmpeg, error_args, error_output))
                # Clean up failed temporary file
                os.remove(final_filename)
            else:
                self.logger.error('FFmpeg finalize failed. Command: {} {}. No output file created.'.format(
                    self.backends.ffmpeg, args))
            return False

    def cut(self, source: str, output: str, frametime: str, duration: str, allstreams: bool=True, vcodec: str=None,
            run: bool=True) -> Union[bool, str]:
        self.logger.info('[DEBUG] Cut: Starting cut from {}s duration {}s, allstreams={}'.format(frametime, duration, allstreams))
        self.checkDiskSpace(output)
        stream_map = self.parseMappings(allstreams)
        self.logger.info('[DEBUG] Cut: Using stream map: "{}"'.format(stream_map.strip()))
        if vcodec is not None:
            encode_options = VideoService.config.encoding.get(vcodec, vcodec)
            args = '-v 32 -i "{}" -ss {} -t {} -c:v {} -c:a copy -c:s copy {}-avoid_negative_ts 1 ' \
                   '-y "{}"'.format(source, frametime, duration, encode_options, stream_map, output)
        else:
            args = '-v error -ss {} -t {} -i "{}" -c copy {}-avoid_negative_ts 1 -y "{}"' \
                   .format(frametime, duration, source, stream_map, output)
        if run:
            self.logger.info('[DEBUG] Cut: Executing ffmpeg with args: {}'.format(args))
            result = self.cmdExec(self.backends.ffmpeg, args)
            output_size = os.path.getsize(output) if os.path.exists(output) else 0
            self.logger.info('[DEBUG] Cut: First attempt result={}, output size={}'.format(result, output_size))
            if not result or output_size < 1000:
                if allstreams:
                    self.logger.info('[DEBUG] Cut: First attempt failed, retrying with essential streams only')
                    # cut failed so try again without mapping all media streams but preserve video and audio
                    self.logger.info('cut resulted in zero length file, trying again with essential streams only (video + audio)')
                    return self.cut(source, output, frametime, duration, False)
                else:
                    # both attempts to cut have failed so exit and let user know
                    VideoService.cleanup([output])
                    return False
            return True
        else:
            if os.getenv('DEBUG', False) or getattr(self.parent, 'verboseLogs', False):
                self.logger.info(args)
            return args

    def smartinit(self, clips: int):
        self.smartcut_jobs = []
        # noinspection PyUnusedLocal
        [
            self.smartcut_jobs.append(Munch(output='', bitrate=0, allstreams=True, procs={}, files={}, results={}))
            for index in range(clips)
        ]

    def calculateSmartCutSteps(self, source: str, clipTimes: list) -> int:
        """Calculate the exact number of progress steps needed for SmartCut processing"""
        # Use a conservative estimate to avoid blocking keyframe analysis during UI setup
        # Each clip: start(possible) + middle + end(possible) + join = max 4 steps per clip + final join
        non_external_clips = len([clip for clip in clipTimes if not (len(clip) > 3 and len(clip[3]))])
        
        # For single clip: usually 3-4 steps (middle + join + possible start/end)
        # For multiple clips: add final join step
        # Adding extra buffer to prevent hanging at 100%
        if non_external_clips == 1:
            return 6  # Conservative estimate for single clip with buffer
        else:
            return non_external_clips * 4 + 2  # Conservative estimate with buffer

    def smartcut(self, index: int, source: str, output: str, start: float, end: float, allstreams: bool = True) -> None:
        self.logger.info('[DEBUG] SmartCut: Starting for clip {} - from {}s to {}s'.format(index, start, end))
        output_file, output_ext = os.path.splitext(output)
        
        # Check if user wants fast cuts (skip keyframe analysis entirely for speed)
        fast_mode = getattr(self, 'fast_smartcut', False) or os.getenv('VIDCUTTER_FAST_SMARTCUT', False)
        
        if fast_mode:
            # Ultra-fast mode: skip keyframe analysis, use stream copy with user's exact times
            self.logger.info('[DEBUG] SmartCut: Using ultra-fast mode (stream copy) for clip {}'.format(index))
            self.smartcut_jobs[index].output = output
            self.smartcut_jobs[index].allstreams = allstreams
            self.smartcut_jobs[index].files.update(middle='{0}_middle_{1}{2}'
                                                   .format(output_file, '{0:0>2}'.format(index), output_ext))
            middleproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
            middleproc.setProcessChannelMode(QProcess.MergedChannels)
            middleproc.setWorkingDirectory(os.path.dirname(self.smartcut_jobs[index].files['middle']))
            middleproc.setObjectName('middle.{}'.format(index))
            middleproc.started.connect(lambda: self.progress.emit(index))
            args = self.cut(source=source,
                         output=self.smartcut_jobs[index].files['middle'],
                         frametime=str(start),
                         duration=str(end - start),
                         allstreams=allstreams,
                         run=False)
            self.logger.info('[DEBUG] SmartCut fast mode: Starting middle process with args: {}'.format(args))
            middleproc.setArguments(shlex.split(args))
            self.smartcut_jobs[index].procs.update(middle=middleproc)
            self.smartcut_jobs[index].results.update(middle=False)
            middleproc.start()
            return
        
        self.logger.info('[DEBUG] SmartCut: Getting GOP bisections for clip {}'.format(index))
        bisections = self.getGOPbisections(source, start, end)
        self.logger.info('[DEBUG] SmartCut: GOP bisections complete for clip {}'.format(index))
        self.smartcut_jobs[index].output = output
        self.smartcut_jobs[index].allstreams = allstreams
        
        # Check if we can do a fast keyframe-aligned cut instead of slow re-encoding
        start_keyframe_aligned = abs(bisections['start'][1] - start) < 0.1  # Within 100ms of keyframe
        end_keyframe_aligned = abs(bisections['end'][1] - end) < 0.1  # Within 100ms of keyframe
        
        if start_keyframe_aligned and end_keyframe_aligned:
            # Fast path: both start and end are close to keyframes, use stream copy
            self.logger.info('SmartCut: Using fast keyframe-aligned cut for clip {}'.format(index))
            self.smartcut_jobs[index].files.update(middle='{0}_middle_{1}{2}'
                                                   .format(output_file, '{0:0>2}'.format(index), output_ext))
            middleproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
            middleproc.setProcessChannelMode(QProcess.MergedChannels)
            middleproc.setWorkingDirectory(os.path.dirname(self.smartcut_jobs[index].files['middle']))
            middleproc.setObjectName('middle.{}'.format(index))
            middleproc.started.connect(lambda: self.progress.emit(index))
            middleproc.setArguments(shlex.split(
                self.cut(source=source,
                         output=self.smartcut_jobs[index].files['middle'],
                         frametime=str(bisections['start'][1]),  # Use keyframe time
                         duration=str(bisections['end'][1] - bisections['start'][1]),
                         allstreams=allstreams,
                         run=False)))
            self.smartcut_jobs[index].procs.update(middle=middleproc)
            self.smartcut_jobs[index].results.update(middle=False)
            middleproc.start()
            return
        
        # Slow path: need re-encoding for frame-accurate cuts
        self.logger.info('SmartCut: Using frame-accurate re-encoding for clip {} (may be slow)'.format(index))
        
        # ----------------------[ STEP 1 - start of clip if not starting on a keyframe ]-------------------------
        if bisections['start'][1] > bisections['start'][0]:
            self.smartcut_jobs[index].files.update(start='{0}_start_{1}{2}'
                                                   .format(output_file, '{0:0>2}'.format(index), output_ext))
            startproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
            startproc.setObjectName('start.{}'.format(index))
            startproc.started.connect(lambda: self.progress.emit(index))
            args = self.cut(source=source,
                         output=self.smartcut_jobs[index].files['start'],
                         frametime=str(start),
                         duration=bisections['start'][1] - start,
                         allstreams=allstreams,
                         vcodec=self.streams.video.codec_name,
                         run=False)
            self.logger.info('[DEBUG] SmartCut: Starting START process with args: {}'.format(args))
            startproc.setArguments(shlex.split(args))
            self.smartcut_jobs[index].procs.update(start=startproc)
            self.smartcut_jobs[index].results.update(start=False)
            startproc.start()
        # ----------------------[ STEP 2 - cut middle segment of clip ]-------------------------
        self.smartcut_jobs[index].files.update(middle='{0}_middle_{1}{2}'
                                               .format(output_file, '{0:0>2}'.format(index), output_ext))
        middleproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
        middleproc.setProcessChannelMode(QProcess.MergedChannels)
        middleproc.setWorkingDirectory(os.path.dirname(self.smartcut_jobs[index].files['middle']))
        middleproc.setObjectName('middle.{}'.format(index))
        middleproc.started.connect(lambda: self.progress.emit(index))
        
        # CODEC FIX: Use same codec for all segments to ensure compatibility
        # Since we have start/end segments that use re-encoding, make middle use re-encoding too
        args = self.cut(source=source,
                     output=self.smartcut_jobs[index].files['middle'],
                     frametime=bisections['start'][2],
                     duration=bisections['end'][1] - bisections['start'][2],
                     allstreams=allstreams,
                     vcodec=self.streams.video.codec_name,
                     run=False)
        self.logger.info('[DEBUG] SmartCut: Using re-encoding for MIDDLE to match START/END segments')
        
        self.logger.info('[DEBUG] SmartCut: Preparing MIDDLE process with args: {}'.format(args))
        middleproc.setArguments(shlex.split(args))
        self.smartcut_jobs[index].procs.update(middle=middleproc)
        self.smartcut_jobs[index].results.update(middle=False)
        if len(self.smartcut_jobs[index].procs) == 1:
            self.logger.info('[DEBUG] SmartCut: Starting MIDDLE process immediately (no start segment)')
            middleproc.start()
        else:
            self.logger.info('[DEBUG] SmartCut: MIDDLE process will start after START completes')
        # ----------------------[ STEP 3 - end of clip if not ending on a keyframe ]-------------------------
        if bisections['end'][2] > bisections['end'][1]:
            self.smartcut_jobs[index].files.update(end='{0}_end_{1}{2}'
                                                   .format(output_file, '{0:0>2}'.format(index), output_ext))
            endproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
            endproc.setObjectName('end.{}'.format(index))
            endproc.started.connect(lambda: self.progress.emit(index))
            args = self.cut(source=source,
                         output=self.smartcut_jobs[index].files['end'],
                         frametime=bisections['end'][1],
                         duration=end - bisections['end'][1],
                         allstreams=allstreams,
                         vcodec=self.streams.video.codec_name,
                         run=False)
            self.logger.info('[DEBUG] SmartCut: Preparing END process with args: {}'.format(args))
            endproc.setArguments(shlex.split(args))
            self.smartcut_jobs[index].procs.update(end=endproc)
            self.smartcut_jobs[index].results.update(end=False)
            self.logger.info('[DEBUG] SmartCut: END process will start after MIDDLE completes')

    @pyqtSlot(int, QProcess.ExitStatus)
    def smartcheck(self, code: int, status: QProcess.ExitStatus) -> None:
        if hasattr(self, 'smartcut_jobs') and not self.smartcutError:
            try:
                name, index = self.sender().objectName().split('.')
                index = int(index)
            except (ValueError, AttributeError) as e:
                self.logger.error('SmartCut: Error parsing process name: {}'.format(e))
                return
                
            self.smartcut_jobs[index].results[name] = (code == 0 and status == QProcess.NormalExit)
            self.logger.info('SmartCut: Process {} for clip {} finished with code {} status {}'.format(
                name, index, code, status))
            
            if os.getenv('DEBUG', False) or getattr(self.parent, 'verboseLogs', False):
                self.logger.info('SmartCut progress: {}'.format(self.smartcut_jobs[index].results))
            
            resultfile = self.smartcut_jobs[index].files.get(name)
            if not resultfile:
                self.logger.error('SmartCut: No output file defined for {} segment'.format(name))
                return
                
            if not self.smartcut_jobs[index].results[name] or not os.path.exists(resultfile) or os.path.getsize(resultfile) < 1000:
                args = self.smartcut_jobs[index].procs[name].arguments()
                if '-map' in args:
                    self.logger.info('SmartCut resulted in zero length file, trying again with essential streams only (video + audio)')
                    # Remove all existing -map arguments
                    while '-map' in args:
                        pos = args.index('-map')
                        args.remove('-map')
                        if pos < len(args):
                            del args[pos]
                    # Add essential stream mappings
                    args.extend(['-map', '0:v:0'])  # First video stream
                    if hasattr(self.streams, 'audio') and len(self.streams.audio) > 0:
                        args.extend(['-map', '0:a:0'])  # First audio stream
                    self.smartcut_jobs[index].procs[name].setArguments(args)
                    self.smartcut_jobs[index].procs[name].started.disconnect()
                    self.smartcut_jobs[index].procs[name].start()
                    return
                else:
                    self.smartcutError = True
                    # both attempts to cut have failed so exit and let user know
                    self.logger.error('Error executing: {0} {1}'
                                      .format(self.smartcut_jobs[index].procs[name].program(), args))
                    self.error.emit('SmartCut failed to cut media file. Please ensure your media files are valid '
                                    'otherwise try again with SmartCut disabled.')
                    return
            if False not in self.smartcut_jobs[index].results.values():
                self.logger.info('[DEBUG] SmartCheck: All segments complete for clip {}, starting join'.format(index))
                self.smartjoin(index)
            else:
                self.logger.info('[DEBUG] SmartCheck: Starting next segment for clip {}'.format(index))
                if name == 'start':
                    self.logger.info('[DEBUG] SmartCheck: Starting middle process')
                    self.smartcut_jobs[index].procs['middle'].start()
                elif name == 'middle' and 'end' in self.smartcut_jobs[index].procs:
                    self.logger.info('[DEBUG] SmartCheck: Starting end process')
                    self.smartcut_jobs[index].procs['end'].start()

    def smartabort(self):
        for job in self.smartcut_jobs:
            for name in job.procs:
                if job.procs[name].state() != QProcess.NotRunning:
                    job.procs[name].terminate()
            VideoService.cleanup(job.files)

    def smartjoin(self, index: int) -> None:
        self.logger.info('[DEBUG] SmartJoin: Starting join for clip {}'.format(index))
        self.progress.emit(index)
        final_join = False
        
        # Build joinlist with only existing files
        joinlist = []
        for segment in ['start', 'middle', 'end']:
            filepath = self.smartcut_jobs[index].files.get(segment)
            if filepath and os.path.exists(filepath):
                self.logger.info('[DEBUG] SmartJoin: Found segment {} file: {} (size: {} bytes)'.format(
                    segment, filepath, os.path.getsize(filepath)))
                joinlist.append(filepath)
            else:
                self.logger.info('[DEBUG] SmartJoin: Segment {} not found or doesn\'t exist: {}'.format(
                    segment, filepath if filepath else 'None'))
        
        if not joinlist:
            self.logger.error('[DEBUG] SmartCut: No files to join for clip {}'.format(index))
            self.finished.emit(False, self.smartcut_jobs[index].output)
            return
        
        # If only one file (e.g., just middle), no join needed - just rename
        if len(joinlist) == 1:
            self.logger.info('[DEBUG] SmartJoin: Only one segment, renaming {} to {}'.format(
                joinlist[0], self.smartcut_jobs[index].output))
            try:
                os.rename(joinlist[0], self.smartcut_jobs[index].output)
                final_join = True
                self.logger.info('[DEBUG] SmartJoin: Rename successful')
            except Exception as e:
                self.logger.error('[DEBUG] SmartCut: Failed to rename single segment: {}'.format(e))
                final_join = False
        else:
            # Multiple files need joining
            self.logger.info('[DEBUG] SmartJoin: Multiple segments to join: {}'.format(len(joinlist)))
            if joinlist and self.isMPEGcodec(joinlist[0]):
                self.logger.info('[DEBUG] smartcut files are MPEG based so join via MPEG-TS')
                final_join = self.mpegtsJoin(joinlist, self.smartcut_jobs[index].output, None)
            if not final_join:
                self.logger.info('[DEBUG] smartcut MPEG-TS join failed, retry with standard concat')
                final_join = self.join(joinlist, self.smartcut_jobs[index].output,
                                       self.smartcut_jobs[index].allstreams, None)
            self.logger.info('[DEBUG] SmartJoin: Join result = {}'.format(final_join))
        
        self.logger.info('[DEBUG] SmartJoin: Cleaning up temporary files')
        VideoService.cleanup(joinlist)
        self.logger.info('[DEBUG] SmartJoin: Emitting finished signal with result={}'.format(final_join))
        self.finished.emit(final_join, self.smartcut_jobs[index].output)
        self.logger.info('[DEBUG] SmartJoin: Complete for clip {}'.format(index))

    @staticmethod
    def cleanup(files: List[str]) -> None:
        try:
            [os.remove(file) for file in files]
        except FileNotFoundError:
            pass

    def join(self, inputs: List[str], output: str, allstreams: bool=True, chapters: Optional[List[str]]=None) -> bool:
        self.logger.info('[DEBUG] Join: Starting join with {} inputs to {}'.format(len(inputs), output))
        self.logger.info('[DEBUG] Join: Input files: {}'.format(inputs))
        self.checkDiskSpace(output)
        filelist = os.path.normpath(os.path.join(os.path.dirname(inputs[0]), '_vidcutter.list'))
        self.logger.info('[DEBUG] Join: Creating concat list file: {}'.format(filelist))
        with open(filelist, 'w') as f:
            for file in inputs:
                if os.path.exists(file):
                    self.logger.info('[DEBUG] Join: Adding to list: {} (size: {} bytes)'.format(
                        file, os.path.getsize(file)))
                    f.write('file \'{}\'\n'.format(file.replace("'", "\\'"))) 
                else:
                    self.logger.error('[DEBUG] Join: File does not exist: {}'.format(file))
        
        # Show contents of concat list file
        with open(filelist, 'r') as f:
            list_content = f.read()
            self.logger.info('[DEBUG] Join: Concat list contents:\n{}'.format(list_content))
        # SUBTITLE FIX: Exclude problematic subtitle streams that cause concat failures
        if allstreams:
            # Use essential streams only to avoid subtitle codec incompatibility
            stream_map = '-map 0:v -map 0:a '
        else:
            stream_map = ''
        ffmetadata = None
        if chapters is not None and len(chapters):
            ffmetadata = self.getChapterFile(inputs, chapters)
            metadata = '-i "{}" -map_metadata 1 '.format(ffmetadata)
        else:
            metadata = ''
        args = '-v error -f concat -safe 0 -i "{0}" {1}-c copy {2}-y "{3}"'
        cmd = args.format(filelist, metadata, stream_map, output)
        self.logger.info('[DEBUG] Join: Executing ffmpeg command: {} {}'.format(self.backends.ffmpeg, cmd))
        
        # Capture stderr to see the actual error message
        if self.proc.state() == QProcess.NotRunning:
            self.proc.setProcessChannelMode(QProcess.MergedChannels)
            self.proc.start(self.backends.ffmpeg, shlex.split('-hide_banner {}'.format(cmd)))
            self.proc.waitForFinished(60000)
            
            all_output = self.proc.readAllStandardOutput().data().decode('utf-8', errors='replace')
            exit_code = self.proc.exitCode()
            
            self.logger.info('[DEBUG] Join: FFmpeg concat exit code: {}'.format(exit_code))
            if all_output.strip():
                self.logger.error('[DEBUG] Join: FFmpeg concat output/error: {}'.format(all_output.strip()))
            
            result = (exit_code == 0 and self.proc.exitStatus() == QProcess.NormalExit)
        else:
            result = False
            exit_code = -1
            self.logger.error('[DEBUG] Join: Process already running, cannot execute concat')
        
        # Check if output file was created successfully
        if os.path.exists(output):
            size = os.path.getsize(output)
            self.logger.info('[DEBUG] Join: Output file created: {} (size: {} bytes)'.format(output, size))
            if size > 1000:  # Reasonable file size
                result = True
                self.logger.info('[DEBUG] Join: File size is good, marking as success')
            else:
                self.logger.error('[DEBUG] Join: Output file too small ({}), marking as failed'.format(size))
                result = False
        else:
            self.logger.error('[DEBUG] Join: Output file was not created: {}'.format(output))
            result = False
        try:
            os.remove(filelist)
        except:
            pass
        if chapters and ffmetadata is not None:
            try:
                os.remove(ffmetadata)
            except:
                pass
        self.logger.info('[DEBUG] Join: Cleanup complete, returning {}'.format(result))
        return result

    def getChapterFile(self, scenes: List[str], titles: List[str]=None) -> str:
        ffmetadata = FFMetadata()
        pos = 0
        for index, scene in enumerate(scenes):
            duration = self.duration(scene)
            end = pos + duration.msecsSinceStartOfDay()
            ffmetadata.add_chapter(pos, end, titles[index])
            pos = end
        ffmetafile = os.path.normpath(os.path.join(os.path.dirname(scenes[0]), 'ffmetadata.txt'))
        with open(ffmetafile, 'w') as f:
            f.write(ffmetadata.output())
        return ffmetafile

    def getBSF(self, source: str) -> tuple:
        vbsf, absf = '', ''
        vcodec, acodec = self.codecs(source)
        if vcodec:
            prefix = '-bsf:v'
            if vcodec == 'hevc':
                vbsf = '{} hevc_mp4toannexb'.format(prefix)
            elif vcodec == 'h264':
                vbsf = '{} h264_mp4toannexb'.format(prefix)
            elif vcodec == 'mpeg4':
                vbsf = '{} mpeg4_unpack_bframes'.format(prefix)
            elif vcodec in {'webm', 'ivf', 'vp9'}:
                vbsf = '{} vp9_superframe'.format(prefix)
        if acodec:
            prefix = '-bsf:a'
            if acodec == 'aac':
                absf = '{} aac_adtstoasc'.format(prefix)
            elif acodec == 'mp3':
                absf = '{} mp3decomp'.format(prefix)
        return vbsf, absf

    def blackdetect(self, min_duration: float) -> None:
        try:
            args = '-f lavfi -i "movie=\'{0}\',blackdetect=d={1:.1f}[out0]" '.format(os.path.basename(self.source),
                                                                                     min_duration)
            args += '-show_entries tags=lavfi.black_start,lavfi.black_end -of default=nw=1 -hide_banner'
            if os.getenv('DEBUG', False) or getattr(self.parent, 'verboseLogs', False):
                self.logger.info('{0} {1}'.format(self.backends.ffprobe, args))
            self.filterproc = VideoService.initProc(self.backends.ffprobe, lambda: self.on_blackdetect(min_duration),
                                                    os.path.dirname(self.source))
            self.filterproc.setArguments(shlex.split(args))
            self.filterproc.start()
        except FileNotFoundError:
            self.logger.exception('Could not find media file: {}'.format(self.source), exc_info=True)
            raise

    def on_blackdetect(self, min_duration: float) -> None:
        if self.filterproc.exitStatus() == QProcess.NormalExit and self.filterproc.exitCode() == 0:
            scenes = [[QTime(0, 0)]]
            results = self.filterproc.readAllStandardOutput().data().decode().strip()
            for line in results.split('\n'):
                if re.match(r'\[blackdetect @ (.*)\]', line):
                    vals = line.split(']')[1].strip().split(' ')
                    start = float(vals[0].replace('black_start:', ''))
                    end = float(vals[1].replace('black_end:', ''))
                    dur = float(vals[2].replace('black_duration:', ''))
                    if dur >= min_duration:
                        scenes[len(scenes) - 1].append(self.parent.delta2QTime(start))
                        scenes.append([self.parent.delta2QTime(end)])
            last = scenes[len(scenes) - 1][0]
            dur = self.duration()
            if last < dur and (last.msecsTo(dur) / 1000) >= min_duration:
                scenes[len(scenes) - 1].append(dur)
            else:
                scenes.pop()
            if os.getenv('DEBUG', False) or getattr(self.parent, 'verboseLogs', False):
                self.logger.info(scenes)
            self.addScenes.emit(scenes)

    def killFilterProc(self) -> None:
        if hasattr(self, 'filterproc') and self.filterproc.state() != QProcess.NotRunning:
            self.filterproc.kill()

    def probe(self, source: str) -> Munch:
        try:
            args = '-v error -show_streams -show_format -of json "{}"'.format(source)
            json_data = self.cmdExec(self.backends.ffprobe, args, output=True, mergechannels=False)
            return Munch.fromDict(loads(json_data))
        except FileNotFoundError:
            self.logger.exception('FFprobe could not find media file: {}'.format(source), exc_info=True)
            raise
        except JSONDecodeError:
            self.logger.exception('FFprobe JSON decoding error', exc_info=True)
            raise

    def getKeyframes(self, source: str, formatted_time: bool = False) -> list:
        if len(self.keyframes) and source == self.source:
            self.logger.info('[DEBUG] getKeyframes: Using cached keyframes ({} frames)'.format(len(self.keyframes)))
            return self.keyframes
        
        self.logger.info('[DEBUG] getKeyframes: Starting keyframe analysis for: {}'.format(source))
        self.logger.info('[DEBUG] getKeyframes: This may take some time for large files...')
            
        timecode = '0:00:00.000000' if formatted_time else 0
        args = '-v error -show_packets -select_streams v -show_entries packet=pts_time,flags ' \
               '{0}-of csv "{1}"'.format('-sexagesimal ' if formatted_time else '', source)
        
        # Use shorter timeout for keyframe analysis (30 seconds) to avoid hanging
        # If it takes longer, we'll use fast mode instead
        self.logger.info('[DEBUG] getKeyframes: Running ffprobe for keyframe analysis (30s timeout)')
        result = self.cmdExec(self.backends.ffprobe, args, output=True, suppresslog=True, 
                             mergechannels=False, timeout=30000)
        self.logger.info('[DEBUG] getKeyframes: ffprobe completed, result length: {}'.format(len(result) if result else 0))
        
        if not result:
            self.logger.warning('Keyframe analysis timed out or failed - enabling fast SmartCut mode')
            # Enable fast mode to avoid slow re-encoding
            self.fast_smartcut = True
            # Return minimal keyframes based on duration
            duration_seconds = self.duration().msecsSinceStartOfDay() / 1000.0
            # Create keyframes every 2 seconds as fallback
            keyframe_times = [i * 2.0 for i in range(int(duration_seconds / 2) + 1)]
            keyframe_times.append(duration_seconds)
            if source == self.source and not formatted_time:
                self.keyframes = keyframe_times
            return keyframe_times
            
        keyframe_times = []
        for line in result.split('\n'):
            if line.split(',')[1] != 'N/A':
                timecode = line.split(',')[1]
            if re.search(',K', line):
                if formatted_time:
                    keyframe_times.append(timecode[:-3])
                else:
                    keyframe_times.append(float(timecode))
        
        if not keyframe_times:
            self.logger.warning('No keyframes found - using fallback keyframe timing')
            duration_seconds = self.duration().msecsSinceStartOfDay() / 1000.0
            keyframe_times = [i * 2.0 for i in range(int(duration_seconds / 2) + 1)]
            keyframe_times.append(duration_seconds)
        else:
            duration_seconds = self.duration().msecsSinceStartOfDay() / 1000.0
            if keyframe_times[-1] != duration_seconds:
                keyframe_times.append(duration_seconds)
                
        if source == self.source and not formatted_time:
            self.keyframes = keyframe_times
        return keyframe_times

    def getGOPbisections(self, source: str, start: float, end: float) -> dict:
        self.logger.info('[DEBUG] getGOPbisections: Getting keyframes for {}s to {}s'.format(start, end))
        keyframes = self.getKeyframes(source)
        self.logger.info('[DEBUG] getGOPbisections: Got {} keyframes'.format(len(keyframes)))
        if not keyframes:
            # Fallback if no keyframes available
            return {
                'start': (start, start, start),
                'end': (end, end, end)
            }
            
        start_pos = bisect_left(keyframes, start)
        end_pos = bisect_left(keyframes, end)
        
        # Ensure indices are within bounds
        start_pos = min(start_pos, len(keyframes) - 1)
        end_pos = min(end_pos, len(keyframes) - 1)
        
        return {
            'start': (
                keyframes[start_pos - 1] if start_pos > 0 else keyframes[start_pos],
                keyframes[start_pos],
                keyframes[start_pos + 1] if start_pos < len(keyframes) - 1 else keyframes[start_pos]
            ),
            'end': (
                keyframes[end_pos - 2] if end_pos > 1 and end_pos != len(keyframes) - 1 else keyframes[max(0, end_pos - 1)],
                keyframes[end_pos - 1] if end_pos > 0 and end_pos != len(keyframes) - 1 else keyframes[min(len(keyframes) - 1, end_pos)],
                keyframes[end_pos] if end_pos < len(keyframes) else keyframes[len(keyframes) - 1]
            )
        }

    def isMPEGcodec(self, source: str = None) -> bool:
        if source is None and hasattr(self.streams, 'video'):
            vcodec = self.streams.video.codec_name
            acodec = self.streams.audio[0].codec_name if len(self.streams.audio) > 0 else None
        else:
            vcodec, acodec = self.codecs(source)
            if vcodec is None:
                return False
            if vcodec == 'mpeg4' and os.path.splitext(source)[1] == '.avi':
                return False
        
        # MPEG-TS join not compatible with PCM audio - it gets converted to binary data
        if acodec and acodec.startswith('pcm_'):
            return False
            
        return vcodec.lower() in VideoService.config.mpeg_formats

    # noinspection PyBroadException
    def mpegtsJoin(self, inputs: list, output: str, chapters: Optional[List[str]]=None) -> bool:
        result = False
        try:
            self.checkDiskSpace(output)
            outfiles = []
            video_bsf, audio_bsf = self.getBSF(inputs[0])
            # 1. transcode to mpeg transport streams
            for file in inputs:
                name, _ = os.path.splitext(file)
                outfile = '{}.ts'.format(name)
                outfiles.append(outfile)
                if os.path.isfile(outfile):
                    os.remove(outfile)
                args = '-v error -i "{0}" -c copy -map 0 {1} -f mpegts "{2}"'.format(file, video_bsf, outfile)
                if not self.cmdExec(self.backends.ffmpeg, args):
                    return result
            # 2. losslessly concatenate at the file level
            if len(outfiles):
                if os.path.isfile(output):
                    os.remove(output)
                ffmetadata = None
                if chapters is not None and len(chapters):
                    ffmetadata = self.getChapterFile(outfiles, chapters)
                    metadata = '-i "{}" -map_metadata 1 '.format(ffmetadata)
                else:
                    metadata = ''
                args = '-v error -i "concat:{0}" {1}-c copy {2} "{3}"' \
                       .format("|".join(map(str, outfiles)), metadata, audio_bsf, output)
                result = self.cmdExec(self.backends.ffmpeg, args)
                # 3. cleanup mpegts files
                [os.remove(file) for file in outfiles]
                if chapters and ffmetadata is not None:
                    os.remove(ffmetadata)
        except BaseException:
            self.logger.exception('Exception during MPEG-TS join', exc_info=True)
            result = False
        return result

    def version(self) -> str:
        args = '-version'
        result = self.cmdExec(self.backends.ffmpeg, args, True)
        return re.search(r'ffmpeg\sversion\s([\S]+)\s', result).group(1)

    def mediainfo(self, source: str, output: str = 'HTML') -> str:
        args = '--output={0} "{1}"'.format(output, source)
        return self.cmdExec(self.backends.mediainfo, args, True, True)

    def cmdExec(self, cmd: str, args: str=None, output: bool=False, suppresslog: bool=False, workdir: str=None,
                mergechannels: bool=True, timeout: int=60000):
        if self.proc.state() == QProcess.NotRunning:
            if cmd == self.backends.mediainfo or not mergechannels:
                self.proc.setProcessChannelMode(QProcess.SeparateChannels)
            if cmd in {self.backends.ffmpeg, self.backends.ffprobe}:
                args = '-hide_banner {}'.format(args)
            self.logger.info('[DEBUG] cmdExec: Starting process: {} {}'.format(cmd, args if args is not None else ''))
            self.proc.setWorkingDirectory(workdir if workdir is not None else VideoService.getAppPath())
            self.proc.start(cmd, shlex.split(args))
            self.proc.readyReadStandardOutput.connect(
                partial(self.cmdOut, self.proc.readAllStandardOutput().data().decode().strip()))
            
            self.logger.info('[DEBUG] cmdExec: Waiting for process to finish (timeout={}ms)'.format(timeout))
            # Use timeout to prevent infinite hanging, especially for keyframe analysis
            if not self.proc.waitForFinished(timeout):
                self.logger.warning('[DEBUG] Process timed out after {}ms, terminating: {} {}'.format(timeout, cmd, args))
                self.proc.terminate()
                if not self.proc.waitForFinished(5000):  # Wait 5 seconds for termination
                    self.proc.kill()
                return False if not output else ''
            self.logger.info('[DEBUG] cmdExec: Process finished successfully')
                
            if cmd == self.backends.mediainfo or not mergechannels:
                self.proc.setProcessChannelMode(QProcess.MergedChannels)
            if output:
                cmdoutput = self.proc.readAllStandardOutput().data().decode().strip()
                if getattr(self.parent, 'verboseLogs', False) and not suppresslog:
                    self.logger.info('cmd output: {}'.format(cmdoutput))
                return cmdoutput
            return self.proc.exitStatus() == QProcess.NormalExit and self.proc.exitCode() == 0
        return False

    @pyqtSlot(str)
    def cmdOut(self, output: str) -> None:
        if len(output):
            self.logger.info(output)

    @pyqtSlot(QProcess.ProcessError)
    def cmdError(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.Crashed:
            QMessageBox.critical(self.parent, 'Error alert',
                                 '<h4>{0} Error:</h4><p>{1}</p>'.format(self.backends.ffmpeg, self.proc.errorString()),
                                 buttons=QMessageBox.Close)

    # noinspection PyUnresolvedReferences, PyProtectedMember
    @staticmethod
    def getAppPath(path: str = None) -> str:
        if VideoService.frozen and getattr(sys, '_MEIPASS', False):
            app_path = sys._MEIPASS
        else:
            app_path = os.path.dirname(os.path.realpath(sys.argv[0]))
        return app_path if path is None else os.path.join(app_path, path)
