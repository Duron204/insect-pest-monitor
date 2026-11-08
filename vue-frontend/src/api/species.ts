import { get } from './client'
import type { SpeciesDisclaimerOut, SpeciesOut } from '@/types'

/* ---------------------------- 虫害知识库 ---------------------------- */

export function listSpecies(): Promise<SpeciesOut[]> {
  return get<SpeciesOut[]>('/species')
}

export function speciesDisclaimer(): Promise<SpeciesDisclaimerOut> {
  return get<SpeciesDisclaimerOut>('/species/disclaimer')
}
