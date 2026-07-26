#!/bin/bash
cd "$(dirname "$0")"
chmod +x ./build-macos-arm64.sh
./build-macos-arm64.sh
STATUS=$?
echo
if [[ $STATUS -eq 0 ]]; then
  echo "The Mac installer is ready in versions/macos/v2.4.0."
else
  echo "The build stopped with exit code $STATUS. Review the message above."
fi
read -r -n 1 -s -p "Press any key to close..."
echo
exit $STATUS
