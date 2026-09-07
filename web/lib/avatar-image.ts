/**
 * Client-side crop and re-encode for avatar uploads.
 *
 * This exists to keep the request inside the 64 KiB body cap the proxy and the server both enforce,
 * not to make the image safe -- the server decodes and re-encodes everything it receives regardless,
 * because a client is not a trust boundary.
 */

/** Must stay under the request body cap enforced by the proxy and the ASGI guard. */
export const AVATAR_MAX_BYTES = 32 * 1024;
export const AVATAR_EDGE = 256;

const QUALITY_LADDER = [0.86, 0.74, 0.62, 0.5];
const ACCEPTED = new Set(["image/png", "image/jpeg", "image/webp"]);

export class AvatarPrepareError extends Error {}

export function isAcceptedAvatarType(type: string): boolean {
  return ACCEPTED.has(type.toLowerCase());
}

/**
 * Decode a chosen file, crop it to a centred square, and encode a bounded WebP.
 *
 * The decode goes through `createImageBitmap` so a file that is not really an image fails here with
 * a clear message rather than as a server rejection after a pointless upload.
 */
export async function prepareAvatar(file: File): Promise<Blob> {
  if (!isAcceptedAvatarType(file.type)) {
    throw new AvatarPrepareError("Choose a PNG, JPEG, or WebP image.");
  }

  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new AvatarPrepareError("That file could not be read as an image.");
  }

  try {
    const edge = Math.min(bitmap.width, bitmap.height);
    if (edge <= 0) throw new AvatarPrepareError("That image has no visible content.");
    const left = Math.floor((bitmap.width - edge) / 2);
    const top = Math.floor((bitmap.height - edge) / 2);

    const canvas = document.createElement("canvas");
    canvas.width = AVATAR_EDGE;
    canvas.height = AVATAR_EDGE;
    const context = canvas.getContext("2d");
    if (!context) throw new AvatarPrepareError("This browser cannot process images.");
    context.imageSmoothingQuality = "high";
    context.drawImage(bitmap, left, top, edge, edge, 0, 0, AVATAR_EDGE, AVATAR_EDGE);

    for (const quality of QUALITY_LADDER) {
      const blob = await toBlob(canvas, quality);
      if (blob && blob.size <= AVATAR_MAX_BYTES) return blob;
    }
    throw new AvatarPrepareError("That image could not be compressed small enough.");
  } finally {
    bitmap.close();
  }
}

function toBlob(canvas: HTMLCanvasElement, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, "image/webp", quality));
}
