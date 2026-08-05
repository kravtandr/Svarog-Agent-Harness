import { useEffect, useState } from "react";

import type { Api } from "../api/client";
import type { SkillCard } from "../api/types";
import { counted } from "../model/plural";
import { riskClass, riskLabel } from "../model/risk";
import "./SkillsScreen.css";

/**
 * Скиллы: карточки так, как их видит агент при подборе.
 *
 * Читается из тех же каталогов, что и в CLI (`skills.paths`), поэтому
 * список здесь и в `svarog skills list` совпадает по построению.
 */
export function SkillsScreen({ api }: { api: Api }) {
  const [skills, setSkills] = useState<SkillCard[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .skills()
      .then(setSkills)
      .catch(() => setError("Не удалось загрузить скиллы."));
  }, [api]);

  if (error !== null) return <p className="skills__error">{error}</p>;
  if (skills === null) return <p className="skills__hint">Загружаем скиллы…</p>;
  if (skills.length === 0)
    return (
      <p className="skills__hint">
        Скиллов пока нет — положите их в каталог skills, и Сварог начнёт их
        подбирать.
      </p>
    );

  return (
    <div className="skills">
      <p className="skills__count">
        {counted(skills.length, "скилл", "скилла", "скиллов")}
      </p>
      {skills.map((skill) => (
        <article key={skill.name} className="skill">
          <header className="skill__head">
            <h3 className="skill__name">{skill.name}</h3>
            <span className="skill__version">{skill.version}</span>
            <span className={`skill__risk ${riskClass(skill.risk)}`}>
              {riskLabel(skill.risk)}
            </span>
          </header>
          <p className="skill__description">{skill.description}</p>
        </article>
      ))}
    </div>
  );
}
