Drop reference PNGs here. Filename becomes the label, e.g.
  itchy_scratchy_title.png   (the I&S title card)
  channel6_desk.png          (Kent at the news desk)
  troy_mcclure.png
Grab them with: ffmpeg -ss HH:MM:SS -i episode.mkv -frames:v 1 refs/name.png
