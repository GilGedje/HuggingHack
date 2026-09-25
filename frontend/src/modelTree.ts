/** Words for how a model relates to the model it was made from. */

type Relation = 'quantized' | 'finetune' | 'adapter' | 'merge'

export const RELATIONS: Relation[] = ['quantized', 'finetune', 'adapter', 'merge']

/** "Quantization of Qwen/Qwen3-0.6B" */
export function relationOf(relation: string | null | undefined): string {
  return { quantized: 'Quantization of', finetune: 'Fine-tune of', adapter: 'Adapter for', merge: 'Merge of' }[relation || ''] || 'Made from'
}

/** A group of models made from one: "Quantizations", "Fine-tunes". */
export function relationGroup(relation: string, count: number): string {
  const one = { quantized: 'Quantization', finetune: 'Fine-tune', adapter: 'Adapter', merge: 'Merge' }[relation] || 'Derived model'
  return count === 1 ? one : `${one}s`
}

/** The kind itself, for a picker: "Quantization". */
export function relationName(relation: string): string {
  return relationGroup(relation, 1)
}
