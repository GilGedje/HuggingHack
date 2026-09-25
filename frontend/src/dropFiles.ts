export interface DroppedFile {
  file: File
  /** Path inside the dropped folder, with forward slashes. */
  path: string
}

/** The file system entries of a drop. Take them before the first await: the
 * drop's data expires once the event handler yields. */
export function droppedEntries(dataTransfer: DataTransfer): FileSystemEntry[] {
  return Array.from(dataTransfer.items)
    .map((item) => item.webkitGetAsEntry?.())
    .filter((entry): entry is FileSystemEntry => Boolean(entry))
}

function readEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject))
}

/** Every file under dropped folders and files. A single dropped folder is the
 * repository itself, so its own name is left out of the paths. */
export async function readDrop(entries: FileSystemEntry[]): Promise<DroppedFile[]> {
  const files: DroppedFile[] = []
  async function walk(entry: FileSystemEntry, prefix: string): Promise<void> {
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) =>
        (entry as FileSystemFileEntry).file(resolve, reject),
      )
      files.push({ file, path: prefix + entry.name })
      return
    }
    for (const child of await children(entry as FileSystemDirectoryEntry)) {
      await walk(child, `${prefix}${entry.name}/`)
    }
  }
  async function children(directory: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
    const reader = directory.createReader()
    const all: FileSystemEntry[] = []
    // Browsers hand out directory listings in batches until an empty one.
    for (let batch = await readEntries(reader); batch.length; batch = await readEntries(reader)) {
      all.push(...batch)
    }
    return all
  }
  if (entries.length === 1 && entries[0].isDirectory) {
    for (const child of await children(entries[0] as FileSystemDirectoryEntry)) await walk(child, '')
  } else {
    for (const entry of entries) await walk(entry, '')
  }
  return files
}
