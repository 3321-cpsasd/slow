from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterSectionPolicy:
    """Technical guardrails and user-facing guidance for chapter sizing.

    Section count is not a semantic rule. The typical range supports a useful
    workload hint, while the technical range only rejects obviously malformed
    model output.
    """

    technical_min: int = 2
    typical_min: int = 3
    typical_max: int = 5
    technical_max: int = 12

    def workload(self, section_count: int) -> dict:
        if not self.technical_min <= section_count <= self.technical_max:
            level = "anomalous"
            message = "本章小节数量不符合当前技术护栏；已保留历史数据，需要显式检查。"
        elif section_count < self.typical_min:
            level = "light"
            message = "本章内容较精简，通常适合已有较高掌握度或目标较简单的情况。"
        elif section_count > self.typical_max:
            level = "extended"
            message = "本章超过典型小节数量，预计需要更多学习时间；不会因此自动拆章。"
        else:
            level = "typical"
            message = "本章处于典型学习工作量范围。"
        return {
            "level": level,
            "sectionCount": section_count,
            "typicalRange": [self.typical_min, self.typical_max],
            "technicalRange": [self.technical_min, self.technical_max],
            "message": message,
        }


CHAPTER_SECTION_POLICY = ChapterSectionPolicy()
