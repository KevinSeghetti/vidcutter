#!/bin/bash

echo "VidCutter SmartCut Performance Fix"
echo "==================================="
echo ""
echo "CRITICAL BUG FIXES APPLIED:"
echo "- Fixed NoneType crash when videos have no audio stream"
echo "- Added timeout protection for keyframe analysis"
echo "- Improved error handling in join operations"
echo "- Added fast SmartCut mode to bypass re-encoding"
echo ""
echo "This script enables fast SmartCut mode to fix performance issues."
echo ""
echo "Fast mode uses stream copying instead of re-encoding, which is MUCH faster"
echo "but may be slightly less frame-accurate (off by a few frames at cut points)."
echo ""
echo "To enable fast SmartCut mode, run VidCutter with:"
echo ""
echo "  export VIDCUTTER_FAST_SMARTCUT=1"
echo "  vidcutter"
echo ""
echo "Or you can disable SmartCut entirely in the VidCutter settings."
echo ""
echo "Starting VidCutter with fast SmartCut and debug logging enabled..."
echo ""

export VIDCUTTER_FAST_SMARTCUT=1
export DEBUG=1  # Enable debug logging to see what's happening

# Try to find vidcutter executable
if command -v vidcutter &> /dev/null; then
    vidcutter
elif [ -f "./vidcutter/__main__.py" ]; then
    python3 -m vidcutter
else
    echo "Could not find VidCutter executable."
    echo "Please run with: python3 -m vidcutter"
fi