/** Approved 2026-11-13 About copy: Russian is editorially fixed, not machine-translated.
 * English strings live in backend/localization/en.json and are translated for
 * all other UI languages by the existing Localizer. Keep the keys in sync.
 */
export const ABOUT_SECTIONS: readonly {
  heading: string;
  blocks: readonly { key: string; bullet: boolean; emphasis: boolean }[];
}[] = [
  {
    "heading": "about.s1.h",
    "blocks": [
      {
        "key": "about.s1.p1",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s1.p2",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s1.p3",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s1.p4",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s1.p5",
        "bullet": false,
        "emphasis": true
      }
    ]
  },
  {
    "heading": "about.s2.h",
    "blocks": [
      {
        "key": "about.s2.p1",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s2.p2",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s2.p3",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s2.p4",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s2.p5",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s2.p6",
        "bullet": false,
        "emphasis": false
      }
    ]
  },
  {
    "heading": "about.s3.h",
    "blocks": [
      {
        "key": "about.s3.p1",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s3.b1",
        "bullet": true,
        "emphasis": false
      },
      {
        "key": "about.s3.b2",
        "bullet": true,
        "emphasis": false
      },
      {
        "key": "about.s3.b3",
        "bullet": true,
        "emphasis": false
      },
      {
        "key": "about.s3.b4",
        "bullet": true,
        "emphasis": false
      },
      {
        "key": "about.s3.p2",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s3.p3",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s3.p4",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s3.p5",
        "bullet": false,
        "emphasis": false
      }
    ]
  },
  {
    "heading": "about.s4.h",
    "blocks": [
      {
        "key": "about.s4.p1",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s4.p2",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s4.p3",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s4.p4",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s4.p5",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s4.p6",
        "bullet": false,
        "emphasis": false
      }
    ]
  },
  {
    "heading": "about.s5.h",
    "blocks": [
      {
        "key": "about.s5.p1",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p2",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p3",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p4",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p5",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p6",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p7",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p8",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p9",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s5.p10",
        "bullet": false,
        "emphasis": true
      }
    ]
  },
  {
    "heading": "about.s6.h",
    "blocks": [
      {
        "key": "about.s6.p1",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s6.p2",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s6.p3",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s6.p4",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s6.p5",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s6.p6",
        "bullet": false,
        "emphasis": false
      },
      {
        "key": "about.s6.p7",
        "bullet": false,
        "emphasis": true
      }
    ]
  }
];

export const APPROVED_ABOUT_RU: Readonly<Record<string, string>> = {
  "about.title": "Об игре",
  "about.s1.h": "Думаешь, ты умнее искусственного интеллекта?",
  "about.s1.p1": "Прекрасно. У тебя есть возможность это доказать.",
  "about.s1.p2": "Мы создали игру, в которой искусственный интеллект задаёт вопросы, а человек пытается на них ответить.",
  "about.s1.p3": "Казалось бы, что может быть проще?",
  "about.s1.p4": "Но есть одна проблема: искусственный интеллект знает невероятно много. И ещё одна: иногда ему только кажется, что он всё знает.",
  "about.s1.p5": "Вот и посмотрим, кто из вас умнее.",
  "about.s2.h": "15 вопросов. Четыре варианта. Один победитель.",
  "about.s2.p1": "Тебя ждут 15 вопросов с постепенно возрастающей сложностью.",
  "about.s2.p2": "Первые покажутся довольно простыми. Не расслабляйся. Дальше будут вопросы, над которыми придётся серьёзно подумать. А ближе к финалу даже весьма образованный человек может обнаружить неожиданные пробелы в своих знаниях.",
  "about.s2.p3": "У каждого вопроса четыре варианта ответа. Выбери правильный — и двигайся дальше.",
  "about.s2.p4": "Ошибся? Игра закончена. Но заработанные звёзды останутся с тобой.",
  "about.s2.p5": "А если правильно ответишь на все 15 вопросов, не используя внутриигровые подсказки, тебя ждёт секретный шестнадцатый вопрос.",
  "about.s2.p6": "Он не обещает быть лёгким.",
  "about.s3.h": "Знания должны вознаграждаться",
  "about.s3.p1": "За успешное прохождение этапов ты получаешь звёзды:",
  "about.s3.b1": "🥉 Бронза — за пятый правильный ответ.",
  "about.s3.b2": "🥈 Серебро — за десятый.",
  "about.s3.b3": "🥇 Золото — за пятнадцатый.",
  "about.s3.b4": "💎 Платина — за успешное прохождение секретного шестнадцатого вопроса.",
  "about.s3.p2": "Звёзды — не просто красивые значки. Это игровая валюта.",
  "about.s3.p3": "С их помощью можно получать преимущества: убирать неправильные ответы, менять категорию вопроса, исключать нежелательные категории перед игрой и пользоваться услугами персонального помощника.",
  "about.s3.p4": "Некоторые виды звёзд можно обменивать друг на друга.",
  "about.s3.p5": "Но помни: каждая подсказка имеет свою цену, а иногда лучше довериться собственным знаниям.",
  "about.s4.h": "Искусственный интеллект тоже может помочь. Или запутать.",
  "about.s4.p1": "Перед игрой ты можешь нанять помощника — виртуального специалиста со своим именем, профессией и областями знаний.",
  "about.s4.p2": "Он способен подсказать, объяснить или предположить правильный ответ.",
  "about.s4.p3": "Однако не стоит слепо ему верить.",
  "about.s4.p4": "Твой помощник не всезнающий оракул. Он может сомневаться, ошибаться и даже уверенно заблуждаться — почти как настоящий человек.",
  "about.s4.p5": "И ещё одна важная деталь: исключение категорий перед началом игры и консультации с помощником во время игры не считаются подсказками, которые лишают тебя права на шестнадцатый вопрос.",
  "about.s4.p6": "А вот использование игровых подсказок, например удаления неправильных вариантов ответа, уже влияет на доступ к секретному раунду.",
  "about.s5.h": "А кто проверяет самого искусственного интеллекта?",
  "about.s5.p1": "Хороший вопрос!",
  "about.s5.p2": "Ведь если ИИ составляет вопросы, кто гарантирует, что он сам знает правильный ответ?",
  "about.s5.p3": "Мы стараемся проверять вопросы до того, как они попадут к игрокам. Но искусственный интеллект способен ошибаться — даже когда выглядит совершенно уверенным в себе.",
  "about.s5.p4": "Поэтому у тебя есть право пожаловаться на вопрос.",
  "about.s5.p5": "Если ты считаешь, что правильного ответа среди вариантов нет, ответов несколько или формулировка некорректна, можешь отправить жалобу на рассмотрение.",
  "about.s5.p6": "Жалобу оценивает отдельный ИИ-судья. Он не обязан соглашаться с тем, кто придумал вопрос.",
  "about.s5.p7": "Если жалоба обоснованна, предусмотрено возмещение по правилам игры. Если нет — решение может оказаться не в твою пользу.",
  "about.s5.p8": "У жалоб есть бесплатный лимит и условия подачи. Их можно посмотреть перед подтверждением.",
  "about.s5.p9": "Мы не обещаем, что искусственный интеллект никогда не ошибётся.",
  "about.s5.p10": "Мы обещаем, что с ним можно спорить.",
  "about.s6.h": "Здесь важен не только результат",
  "about.s6.p1": "Можно проиграть на третьем вопросе и узнать что-то новое.",
  "about.s6.p2": "Можно добраться до пятнадцатого и обнаружить, что твои знания гораздо шире, чем ты предполагал.",
  "about.s6.p3": "А можно обыграть ИИ, забрать платиновую звезду и получить полное моральное право смотреть на компьютер с некоторым превосходством.",
  "about.s6.p4": "В любом случае игра задумана не как экзамен, а как интеллектуальное соревнование.",
  "about.s6.p5": "Играй, ошибайся, учись, возвращайся и пробуй снова.",
  "about.s6.p6": "Вопросы меняются, а твой опыт и заработанные награды сохраняются.",
  "about.s6.p7": "И помни: победа над искусственным интеллектом особенно приятна, когда для неё пришлось использовать собственный.",
  "about.giftTitle": "Постой-ка… Ты действительно дочитал до сюда? 🎁",
  "about.gift.p1": "Вот это неожиданно!",
  "about.gift.p2": "Обычно люди пролистывают правила, нажимают «Согласен» и надеются разобраться по ходу дела.",
  "about.gift.p3": "Но ты не из таких.",
  "about.gift.p4": "Ты проявил внимательность, терпение и здоровое любопытство. А именно эти качества мы здесь и ценим.",
  "about.gift.p5": "Поэтому держи небольшой подарок.",
  "about.gift.p6": "Тебе полагается:",
  "about.gift.platinum": "платиновых звёзд",
  "about.gift.gold": "золотых звёзд",
  "about.gift.silver": "серебряных звёзд",
  "about.gift.bronze": "бронзовых звёзд",
  "about.gift.p7": "Да-да, всё сразу. Без подвоха.",
  "about.gift.p8": "Правда, есть одно условие: подарок можно получить только один раз на аккаунт.",
  "about.gift.p9": "Если у тебя ещё нет аккаунта, зарегистрируйся, чтобы забрать награду.",
  "about.gift.p10": "Спасибо, что дочитал.",
  "about.gift.p11": "А теперь иди и покажи этому искусственному интеллекту, кто здесь действительно умеет думать! 😎",
  "about.giftClaim": "Забрать подарок",
  "about.giftClaimed": "Подарок уже получен. Приятной игры!",
  "about.giftLoading": "Проверяем подарок…",
  "about.giftNeedLogin": "Чтобы забрать подарок, войди в аккаунт или зарегистрируйся.",
  "about.giftSignIn": "Создать аккаунт",
  "about.statusFailed": "Не удалось проверить подарок. Закрой окно и попробуй снова.",
  "about.claimFailed": "Не удалось получить подарок. Закрой окно и попробуй ещё раз — повторно звёзды не начислятся.",
  "about.close": "Закрыть",
  "about.open": "Об игре"
};
