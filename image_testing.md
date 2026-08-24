# Image Integration Testing Rules

- Use base64-encoded images for all image tests and requests.
- Accepted formats are JPEG, PNG, and WEBP only.
- Do not upload blank, solid-color, or uniform-variance images.
- Images must contain real visual features such as objects, edges, textures, or shadows.
- Transcode unsupported images to PNG or JPEG and re-detect the MIME type afterward.
- For animated images, use the first frame only.
- Resize large images to reasonable bounds before upload.