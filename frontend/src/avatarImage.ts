/** Turning a chosen picture into a small square one before it is uploaded. */

const SIZE = 256

/** The middle square of an image, as [x, y, side]. */
export function centerSquare(width: number, height: number): [number, number, number] {
  const side = Math.min(width, height)
  return [Math.round((width - side) / 2), Math.round((height - side) / 2), side]
}

/**
 * Crop to the middle square, scale to 256 px, and encode as WebP (PNG where the
 * browser cannot write WebP). Re-encoding also drops photo metadata such as location.
 */
export async function prepareAvatar(file: File): Promise<Blob> {
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    throw new Error('This browser cannot open that picture. Use a PNG, JPEG, or WebP image.')
  }
  const canvas = document.createElement('canvas')
  canvas.width = SIZE
  canvas.height = SIZE
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Could not prepare the picture.')
  const [x, y, side] = centerSquare(bitmap.width, bitmap.height)
  context.imageSmoothingQuality = 'high'
  context.drawImage(bitmap, x, y, side, side, 0, 0, SIZE, SIZE)
  bitmap.close()
  const encode = (type: string, quality?: number) =>
    new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, type, quality))
  const webp = await encode('image/webp', 0.9)
  const blob = webp && webp.type === 'image/webp' ? webp : await encode('image/png')
  if (!blob) throw new Error('Could not prepare the picture.')
  return blob
}
