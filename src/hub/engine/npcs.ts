// The house staff, as people you can walk up to and talk to (walk.ts, E).
//
// They are characters, not reporters: nothing anyone says here is about the
// user's account, a price, a balance or a result, and no line carries a digit
// (test/hubNpcs.test.ts pins it). The hub shows real numbers in exactly one
// place, the HUD; a dealer who said "the house is up tonight" would be a
// number nobody produced (invariant 10). What they CAN talk about is their own
// job on the floor — each one is a real part of the app wearing a casino
// uniform — and themselves, at length. Nobody here pretends the app is a
// casino: the "bets" are orders on Polymarket, and they say so.

export interface Topic {
  /** What you say. */
  ask: string;
  /** What they say back, one beat at a time. */
  say: string[];
  /** Follow-ups this answer opens; without them you're back at the top. */
  next?: Topic[];
}

export interface Persona {
  /** The staff member this is (Worker.name), or 'captain' for Bluey. */
  id: string;
  name: string;
  title: string;
  /** Said when you walk up and press E (one, at random). */
  greet: string[];
  bye: string[];
  topics: Topic[];
  /** Said out loud, unprompted, as you walk past (Skyrim's "barks"). */
  barks: string[];
}

export const PERSONAS: Persona[] = [
  {
    id: 'Ada',
    name: 'Ada Vantablack',
    title: 'Odds Whisperer · Sportsbook',
    greet: [
      'Shh. Shh. The board is listening. Speak in lowercase.',
      'Oh, a guest. The screens said someone would come. They also said "beans", so, mixed record.',
    ],
    bye: ['Go quietly. The noise floor has ears.', 'If a row lights up as you leave, do not wave at it.'],
    barks: [
      'I keep the noise in a jar. The jar is full.',
      'That row lit up for nothing. That row ALSO lit up for nothing. Suspicious.',
      'The cube turned left by itself again.',
    ],
    topics: [
      {
        ask: 'What do you actually do here?',
        say: [
          'The scanners pour every market on the exchange into this room. Thousands of them, all talking at once, all over that wall.',
          'I listen for the ones that whisper. A book that shifts. A price that moves before it should. When a real one comes in, a row on the board lights up.',
          'Most of it is noise. I keep the noise. Someone has to.',
        ],
        next: [
          {
            ask: 'You keep the noise?',
            say: [
              'In a jar. Under my window. It is labelled NOISE.',
              'Some nights it is louder. I filed a ticket with Gus. He says the wheel does the same thing.',
            ],
          },
        ],
      },
      {
        ask: 'Why is it called a sportsbook?',
        say: [
          'Because every casino needs a wall of screens and somebody squinting at it.',
          'We do not take sports bets, mind. These are prediction markets. The screens just liked the name.',
        ],
      },
      {
        ask: 'Why do you whisper?',
        say: ['Because a signal that hears you coming changes its mind.', 'Also I lost my voice yelling at a bond chart in a previous life.'],
      },
    ],
  },
  {
    id: 'Rui',
    name: 'Rui Tabsworth',
    title: 'Keeper of the Fine Print · Sportsbook',
    greet: [
      'Ah! Hold on, let me just — no, that is the wrong tab. Welcome to the book.',
      'Welcome, welcome. Mind the citations, they are load-bearing.',
    ],
    bye: ['I will bookmark this conversation. I will never open it again.', 'Farewell. See footnote.'],
    barks: [
      'I have never closed a tab. I never will.',
      'Source? Source. Source of the source?',
      'Footnote: see footnote.',
    ],
    topics: [
      {
        ask: 'How many tabs do you have open?',
        say: [
          'Enough that the browser now has its own weather.',
          'Somewhere in there is an article I opened on my first night on the floor. I think about it the way sailors think about the sea.',
        ],
      },
      {
        ask: 'What do you read all night?',
        say: [
          'Whatever Ada flags. She hears it; I find out why it happened.',
          'Who settles a market. What the rules really say. Whether "by the end of the day" means the end of the day or the end of somebody\'s day.',
        ],
        next: [
          {
            ask: 'Does that matter much?',
            say: ['It is the only thing that matters. A market is a promise, and promises have fine print.', 'I read the fine print so the fine print does not read you.'],
          },
        ],
      },
      {
        ask: 'Why do you talk in footnotes?',
        say: ['I do not talk in footnotes.¹', '¹ I do.'],
      },
    ],
  },
  {
    id: 'Bo',
    name: 'Bo the Unquenched',
    title: 'Head Dealer · Card Room',
    greet: ['HAH! A NEW FACE! PULL UP A STOOL!', 'YOU. YES, YOU. DO YOU RESPECT A CLEAN SHUFFLE?'],
    bye: ['GO, AND MAY YOUR EDGES STAY SHARP!', 'TELL THE OTHERS BO IS STILL DEALING!'],
    barks: ['IF IT IS NOT SHUFFLED TWICE, IS IT EVEN A STRATEGY?', 'GERALD! BACK IN THE DECK!', 'CARDS UP!'],
    topics: [
      {
        ask: 'Why are you shouting?',
        say: ['THE SHUFFLER IS LOUD!', '...', 'It is not loud. I just like it.'],
      },
      {
        ask: 'What gets dealt here?',
        say: [
          'STRATEGIES! Your scripts, your presets, your profiles. Rules for when to buy and when to walk away.',
          'When one is saved or checked, I deal it out on this felt. A clean hand goes to the shelf. One you switch on goes straight to the high-limit table to work.',
          'A bad hand folds right here. THAT IS THE POINT.',
        ],
      },
      {
        ask: 'Who is Gerald?',
        say: ['Gerald is the ace of spades. Gerald has been in this deck since the doors opened.', 'Do not look directly at Gerald. He gets shy and hides at the bottom. We shuffle him back up every time. It is still Gerald.'],
        next: [
          {
            ask: 'Is it still Gerald, though?',
            say: ['...', 'GET OUT OF MY CARD ROOM.'],
          },
        ],
      },
    ],
  },
  {
    id: 'Kit',
    name: 'Kit Shufflesong',
    title: 'Trainee Dealer (Self-Declared Poet) · Card Room',
    greet: ['Oh! A listener! Would you like to hear my poem? It is about the deck.', 'Hi! Bo says I am not allowed to recite while he deals anymore.'],
    bye: ['Wait, I have a closing couplet — oh, you are already leaving.', 'Bye! I will write you a verse. It will rhyme with shuffle. Badly.'],
    barks: ['O deck, my deck, thou shuffled shuffle...', 'Deal it faster, edge gets vaster. That is how it works. Right? Right.', 'Nothing rhymes with shuffle. Except muffle. I have checked.'],
    topics: [
      {
        ask: 'Let me hear the poem.',
        say: [
          'Ahem. "Upon the felt, both green and tough, / I dealt a rule, it called the bluff."',
          '"And though Bo said the rule was bad, / it was the finest rule I had."',
          'It is a work in progress. Like me. Like all of us.',
        ],
      },
      {
        ask: 'What does a trainee dealer do?',
        say: ['I hold things. I hand Bo things. I deal when he is on his break.', 'I believe that if you deal a strategy hard enough it gets more edge. Bo says that is not how anything works. I am collecting evidence.'],
      },
    ],
  },
  {
    id: 'Quinn',
    name: 'Quinn Hindsight',
    title: 'Historian of Hands · Practice Table',
    greet: ['You are... from now? Fascinating. I rarely go there.', 'Ah, a guest from the out-of-sample. Please wipe your feet.'],
    bye: ['Go safely into the future. Write down what happens. I will want to replay it later.', 'Goodbye. I will remember this perfectly, which is the problem.'],
    barks: ['Every hand worked in the past. That is what worries me.', 'Do not tell me what happens next. I want to be surprised. Statistically.', 'The past is the only table that never closes.'],
    topics: [
      {
        ask: 'What is the practice table?',
        say: [
          'Every strategy gets played here first, against what the markets actually did, before it is allowed near anything real.',
          'When you run a backtest anywhere in the app, its real curve plays out on that display. Green if it kept an edge after fees, red if it did not.',
          'If it only works on one lucky week, we find out here, where it is cheap to find out.',
        ],
        next: [
          {
            ask: 'And if it passes?',
            say: ['Then it has survived the past.', 'The future is ruder. But at least it is not naive.'],
          },
        ],
      },
      {
        ask: 'Why are you afraid of the present?',
        say: ['Because nobody has graded it yet.', 'In the past, every answer is in the back of the book. Out there, the book is still being written, and the author is very bad at foreshadowing.'],
      },
    ],
  },
  {
    id: 'Nova',
    name: 'Nova Overfit',
    title: 'Keeper of the Lucky Streak · Practice Table',
    greet: ['Before you say anything: are you real, or are you a parameter?', 'Oh good, a variable. Stand still, I need to fit you.'],
    bye: ['Do not generalise from this conversation. It was a small sample.', 'Leaving already? Classic out-of-sample behaviour.'],
    barks: ['The slot machine only spins when a replay runs. I have checked twice.', 'I fit a curve to my lunch. It predicted dessert perfectly. Never again.', 'If a backtest is too good, it is lying. Mine is always too good.'],
    topics: [
      {
        ask: 'Why is there a slot machine?',
        say: ['It spins while a backtest replays, and rests when nothing is running. It never pays out. It is the most honest machine in the building.', 'The reels are the history. Every price, every tick, everything the markets did while you were asleep.'],
      },
      {
        ask: 'Why do you think you are a simulation?',
        say: [
          'Everything here works a little too well. The lighting is always flattering.',
          'I think somebody is backtesting me. If I perform too well, they will throw me out for overfitting.',
          'So I have started making small, believable mistakes. Like this conversation.',
        ],
      },
    ],
  },
  {
    id: 'Tess',
    name: 'Tess Farewell',
    title: 'Bet Runner, Very Emotional · High-Limit Table',
    greet: ['Hello! Sorry, I have been crying a little. I just sent one off.', 'Oh! Are you here to see the bets go? It is so beautiful. And sad.'],
    bye: ['Goodbye! I say goodbye to everything that leaves. Including you.', 'Go. Go! Before I get attached.'],
    barks: ['Goodbye, little bet. Be brave. Fill well.', 'Every one of them comes back changed.', 'Into the chute you go. I believe in you.'],
    topics: [
      {
        ask: 'Where do the bets go?',
        say: [
          'They are orders, really. Through that brass chute, out of the building, across the night to the POLYMARKET window.',
          'Some fill. Some sit there waiting for a price that never comes. When one settles our way, the winnings come back through the payout window and the cashiers carry them to the cage.',
        ],
      },
      {
        ask: 'Why are you so sad about it?',
        say: [
          'They are so small when they leave. Just a market, a side, a price.',
          'And they are going out into a market full of professionals.',
          'I write each one a note. The exchange does not read them. I know. I still write them.',
        ],
        next: [
          {
            ask: 'What do the notes say?',
            say: ['"Stay within your limit." "Do not chase." "Call if you get lonely."', 'Standard stuff.'],
          },
        ],
      },
    ],
  },
  {
    id: 'Max',
    name: 'Max Sizewell',
    title: 'Senior Conviction Officer · High-Limit Table',
    greet: ['This. Is. The. One.', 'You look like someone who appreciates a FULL SEND.'],
    bye: ['Remember: fortune favours the bold, and Rex favours the paperwork.', 'Go big. Within the caps. Rex is watching.'],
    barks: ['This is the hand of a lifetime. Again.', 'Rex says no. Rex always says no.', 'Size? Yes.'],
    topics: [
      {
        ask: 'Is every bet the bet of a lifetime?',
        say: ['Every single one.', 'I have had many lifetimes.', 'Rex calls them "incident reports".'],
      },
      {
        ask: 'Why does Rex keep stopping you?',
        say: [
          'Because this house has rails. Caps on every order, caps on the day, a stop if the day goes bad.',
          'I push. The rails push back. The rails always win.',
          'Honestly? Thank goodness for the rails.',
        ],
      },
      {
        ask: 'Have you ever been unsure about anything?',
        say: ['Once. It was a sandwich.', 'I bought the whole sandwich anyway.'],
      },
    ],
  },
  {
    id: 'Goldie',
    name: 'Goldie Coinbiter',
    title: 'Cage Warden · Cashier Cage',
    greet: ['Halt! Open your mouth. ...Fine, you are not a coin. Proceed.', 'Mm. You have the look of someone who has never bitten a coin. Tragic.'],
    bye: ['Go. And if anyone offers you a chip, bite it first.', 'Off you go. Do not touch the green one.'],
    barks: ['*crunch* Real.', '*crunch* Real. *crunch* ...also real.', 'Keep your hands where the chips can see them.'],
    topics: [
      {
        ask: 'Why do you bite the coins?',
        say: ['To see if they are real.', 'In this cage, only real wins get in. A win the market has not settled is not a win, it is a rumour. I do not bite rumours.'],
      },
      {
        ask: 'What is the green one?',
        say: [
          'Cursed.',
          'It showed up one night. Nobody put it there. It hums when the market moves.',
          'Penny wants to count it. I will not let her. Some coins should not be counted.',
        ],
        next: [
          {
            ask: 'Can I bite it?',
            say: ['Absolutely not.', '...', 'Maybe on your birthday.'],
          },
        ],
      },
    ],
  },
  {
    id: 'Penny',
    name: 'Penny Tallymark',
    title: 'Counter of Small Things · Cashier Cage',
    greet: ['Four hundred and — no. No! You made me lose count.', 'Hello. Please stand still. I am counting your shoelaces.'],
    bye: ['Goodbye. One goodbye. Counted.', 'Off you go. I am starting over. Again.'],
    barks: ['Seven hundred and... seven hundred and something.', 'I have counted the carpet roses. There is one extra.', 'Nobody move.'],
    topics: [
      {
        ask: 'What are you counting?',
        say: ['Everything. Coins. Chips. The number of times Max says "the one".', 'Max is winning.'],
      },
      {
        ask: 'Do you ever finish?',
        say: [
          'Once. I finished counting everything in the cage.',
          'Then a new tray came in and I had to start again.',
          'That is the dream, you know. Having to start again because something good happened.',
        ],
      },
      {
        ask: 'What is the extra carpet rose?',
        say: ['That is what I want to know.', 'Rex thinks it is a risk. Gus thinks it is a stain. Ada thinks it is listening. I think it is mocking me.'],
      },
    ],
  },
  {
    id: 'Rex',
    name: 'Rex Hedgeworth',
    title: 'Head of Security (Sunglasses Indoors) · Cashier Cage',
    greet: ['Before we speak, please sign this waiver. Verbally. Say "I accept the risks of small talk."', 'Stop. Is that a loose shoelace? That is a trip hazard. Noted.'],
    bye: ['Walk, do not run. Running is how tails happen.', 'Leave by the marked exit. All other exits are theoretical.'],
    barks: ['That is a tail risk.', 'Everything is a tail risk if you look closely enough. I look very closely.', 'Sunglasses ON in the cage.'],
    topics: [
      {
        ask: 'Why the sunglasses?',
        say: ['Because the chandeliers are very bright and the risks are very dark.', 'Also something fell on Max once. It was his own expectations.'],
      },
      {
        ask: 'What does security do here?',
        say: [
          'I keep the rails on.',
          'Every order has a size limit. Every day has a spending limit. If the day goes badly, buying stops, and closing out never does.',
          'Nobody here, not even the AI agents, can turn those off. Including me. Especially me.',
        ],
        next: [
          {
            ask: 'Not even the AI agents?',
            say: ['Especially not the AI agents.', 'They can only narrow the rails, never widen them. When a rail says no, you will see them hurry to my desk. I put that in their contract. I also put it in their soup.'],
          },
        ],
      },
      {
        ask: 'What sets off the alarm?',
        say: ['A stop rule firing, or the daily stop. Or a settlement that went against us.', 'It is loud on purpose. Nobody should sleep through a loss.'],
      },
    ],
  },
  {
    id: 'Gus',
    name: 'Gus Spinwhistle',
    title: 'Wheel Whisperer · Roulette',
    greet: ['Keep your voice down — the wheel is in a mood.', 'Oh hey. The wheel likes you. I can tell. It hummed a third higher.'],
    bye: ['Bye! Say goodnight to the wheel on your way out. It notices.', 'Mind the ball, it is going through something.'],
    barks: ['There, there. Nobody is going to replace you.', 'The wheel and the marquee are not speaking.', 'You hear that purr? That is contentment.'],
    topics: [
      {
        ask: 'What is the roulette wheel for?',
        say: [
          'It is the optimizer. Try a setting, try another, keep what did better against the past.',
          'It turns slowly all night for show, but it only spins up when an optimizer run you started finishes, or when a schedule you saved changes strategy at the top of the hour.',
          'Nothing in here bets on red. Well. Except the ball, emotionally.',
        ],
      },
      {
        ask: 'Wheels have moods?',
        say: [
          'Everything that spins has a mood.',
          'The wheel is anxious. The ball is smug. The marquee is a teenager.',
          'My job is half croupier and half listening.',
        ],
        next: [
          {
            ask: 'What is the marquee upset about?',
            say: ['It wanted to be a chandelier. Bo told it it did not have the sparkle for it.', 'We are working through it.'],
          },
        ],
      },
    ],
  },
  {
    id: 'Ivy',
    name: 'Ivy Overclock',
    title: 'Croupier, Has Not Slept · Roulette',
    greet: ['hi hello yes i am awake. extremely awake.', 'Do you know what this dial does? Neither do I. Turning it anyway.'],
    bye: ['bye. if you see sleep, tell it i said no.', 'Off you go! I am going to spin this one to eleven.'],
    barks: ['Eleven. Every dial. Eleven.', 'Sleep is just downtime for people.', 'What if we tried... MORE?'],
    topics: [
      {
        ask: 'When did you last sleep?',
        say: ['Define "sleep".', 'Define "last".', 'I blinked slowly before the doors opened. That counted.'],
      },
      {
        ask: 'What happens if you turn everything to eleven?',
        say: [
          'Gus says the wheel gets stressed and the results look amazing for exactly one week of history.',
          'Nova calls it overfitting. I call it ambition.',
          'We are both right. She is more right.',
        ],
      },
    ],
  },
  {
    id: 'captain',
    name: 'Pit Boss Bluey',
    title: 'Boss of the Floor · VIP Lounge',
    greet: ['The pit boss sees all. The pit boss sees you. The pit boss approves of the shoes.', 'At ease, guest. Unless you brought snacks. Then: welcome to the VIP list.'],
    bye: ['Dismissed. Bring snacks next time. That is house policy.', 'Pit boss\'s log: the guest departs. I remain magnificent.'],
    barks: ['Pit boss\'s log: still magnificent.', 'Keep it moving, folks.', 'Who moved my chair? I am the only one who sits in it.'],
    topics: [
      {
        ask: 'What does the pit boss do?',
        say: [
          'I sit on the podium. I watch the floor. Occasionally I point.',
          'Your AI agents wait here in the lounge between their calls, and their approval requests come to me. The staff does the work. The rails keep it safe. I keep morale high, mostly by being this handsome.',
        ],
      },
      {
        ask: 'Is this a real casino?',
        say: [
          'Goodness, no. Nobody here plays against a house.',
          'Every bet on this floor is an order the app actually placed, on paper or with real money, on a prediction market. Nothing in here is staged for show, and when nothing happens, the floor goes quiet.',
        ],
      },
      {
        ask: 'What are you, exactly?',
        say: ['A pit boss.', 'Next question.'],
        next: [
          {
            ask: 'No, but like... species?',
            say: ['Pit boss.', 'It is a very exclusive species. You have to be born a pit boss. Or own a very good hat.'],
          },
        ],
      },
    ],
  },
];

export const PERSONA_BY_ID = new Map(PERSONAS.map((p) => [p.id, p]));

/** For one of the user's own AI agents, who talks through tool calls, not to
 *  you — and whose real calls are the only thing it would be honest to say. */
export function agentPersona(label: string): Persona {
  return {
    id: `agent:${label}`,
    name: label,
    title: 'One of your AI agents',
    greet: ['...'],
    bye: ['...'],
    barks: [],
    topics: [
      {
        ask: 'Hello?',
        say: [
          `${label} doesn't chat. It's one of your AI agents: it talks through its tool calls, and every one of them shows up on this floor as it happens.`,
        ],
      },
    ],
  };
}
