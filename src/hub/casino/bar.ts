import type { CasinoSave } from './wallet';

// The Velvet Tail's bar: cocktails for play chips. Order one at the counter
// (BarSession), the bartender shakes it, and it lands in your hotbar's
// sixth slot to drink like the beer and the vodka, each one with its own
// kick. Play chips buy nothing real, and nothing here touches the wallet.

export type Glass = 'martini' | 'coupe' | 'rocks';
export type Garnish = 'olive' | 'cherry' | 'peel' | 'lime';

export interface Cocktail {
  id: string;
  name: string;
  recipe: string;
  price: number;
  /** The buzz a whole glass puts in you (a beer is ~0.3, a vodka ~0.95). */
  strength: number;
  color: string;
  glass: Glass;
  garnish: Garnish;
}

export const COCKTAILS: readonly Cocktail[] = [
  { id: 'martini', name: 'Dry Martini', recipe: 'gin · dry vermouth · olive', price: 25, strength: 0.34, color: '#e9f0cf', glass: 'martini', garnish: 'olive' },
  { id: 'oldfashioned', name: 'Old Fashioned', recipe: 'rye · bitters · sugar · orange peel', price: 25, strength: 0.32, color: '#7a3a0c', glass: 'rocks', garnish: 'peel' },
  { id: 'manhattan', name: 'Manhattan', recipe: 'rye · sweet vermouth · cherry', price: 30, strength: 0.34, color: '#8e2f1c', glass: 'coupe', garnish: 'cherry' },
  { id: 'cosmo', name: 'Cosmopolitan', recipe: 'vodka · cranberry · lime', price: 20, strength: 0.26, color: '#ec4f82', glass: 'martini', garnish: 'lime' },
  { id: 'negroni', name: 'Negroni', recipe: 'gin · campari · sweet vermouth', price: 30, strength: 0.3, color: '#c81f22', glass: 'rocks', garnish: 'peel' },
  { id: 'velvet', name: 'The Velvet Tail', recipe: 'house special · ask Dot', price: 45, strength: 0.48, color: '#7b2fe0', glass: 'coupe', garnish: 'cherry' },
];

export type OrderResult = { ok: true; drink: Cocktail } | { ok: false; why: string };

/** Pay for a cocktail out of your play chips. */
export function orderCocktail(save: CasinoSave, id: string): OrderResult {
  const drink = COCKTAILS.find((c) => c.id === id);
  if (!drink) return { ok: false, why: 'Not on the menu, hon.' };
  if (save.chips < drink.price) return { ok: false, why: 'Short on chips: the cashier tops you up.' };
  save.chips -= drink.price;
  return { ok: true, drink };
}
