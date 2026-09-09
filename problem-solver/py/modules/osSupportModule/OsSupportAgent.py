"""
Агент актуализации сведений о поддержке операционных систем.

Лабораторная работа №4 по курсу ТиИСПИС.
Вариант: внешний сервис по сети Internet · JSON · интеграция процедурной
и логической моделей · Python.

Условие инициирования: создание дуги принадлежности действия классу
action_check_os_support. Действие порождается программой обработки
сообщений message_processing_program.

Результат: погружённый в базу знаний экземпляр выпуска операционной
системы и выведенное на нём членство операционной системы в классе
concept_supported_operating_system либо concept_unsupported_operating_system.
"""

import datetime
import logging

import requests

from sc_client.constants import sc_types
from sc_client.client import template_search
from sc_client.models import ScAddr, ScLinkContentType, ScTemplate

from sc_kpm import ScAgentClassic, ScKeynodes, ScResult
from sc_kpm.utils import (
    check_edge,
    create_edge,
    create_link,
    create_node,
    delete_edges,
    get_element_by_norole_relation,
    get_link_content_data,
    get_system_idtf,
)
from sc_kpm.utils.action_utils import (
    create_action_answer,
    finish_action_with_status,
    get_action_arguments,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s", datefmt="[%d-%b-%y %H:%M:%S]"
)

API_URL = "https://endoflife.date/api/{product}.json"
REQUEST_TIMEOUT = 7
CACHE_TTL_SECONDS = 900

# Кэш ответов сервиса на время работы агента. Программа обработки сообщений
# ждёт завершения шага ограниченное время, поэтому повторный вопрос об уже
# запрошенном продукте обслуживается без обращения к сети.
_RESPONSE_CACHE: dict = {}

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
EOL_NOT_ANNOUNCED = "не объявлено"


class OsSupportAgent(ScAgentClassic):
    """Sc-агент, погружающий сведения о жизненном цикле операционной системы."""

    def __init__(self):
        super().__init__("action_check_os_support")

    def on_event(self, event_element: ScAddr, event_edge: ScAddr, action_element: ScAddr) -> ScResult:
        result = self.run(action_element)
        is_successful = result == ScResult.OK
        finish_action_with_status(action_element, is_successful)
        self.logger.info(
            "OsSupportAgent finished %s", "successfully" if is_successful else "unsuccessfully")
        return result

    # ------------------------------------------------------------------ #
    # Процедура 1. Точка входа: проверка применимости и общий ход работы  #
    # ------------------------------------------------------------------ #
    def run(self, action_node: ScAddr) -> ScResult:
        self.logger.info("OsSupportAgent started")
        try:
            arguments = get_action_arguments(action_node, 1)
            if not arguments:
                self.logger.info("OsSupportAgent: the action has no message node")
                return ScResult.OK
            message_addr = arguments[0]

            message_class = ScKeynodes.resolve(
                "concept_message_about_support", sc_types.NODE_CONST_CLASS)
            if not check_edge(sc_types.EDGE_ACCESS_VAR_POS_PERM, message_class, message_addr):
                self.logger.info("OsSupportAgent: the message isn't about support")
                return ScResult.OK

            os_addr = self.get_entity(message_addr)
            if not os_addr.is_valid():
                self.logger.info("OsSupportAgent: the message has no entity")
                return ScResult.OK

            os_idtf = get_system_idtf(os_addr)
            product = self.get_link_value(os_addr, "nrel_endoflife_idtf")
            if product is None:
                self.logger.info(
                    "OsSupportAgent: %s has no external source identifier", os_idtf)
                return ScResult.OK
            cycle = self.get_link_value(os_addr, "nrel_endoflife_cycle")

            release = self.fetch_release(product, cycle)
            if release is None:
                self.logger.info(
                    "OsSupportAgent: the external source is unavailable, using cached knowledge")
                return ScResult.OK if self.has_cached_release(os_addr) else ScResult.OK

            self.clear_previous_knowledge(os_addr)
            release_node = self.immerse_release(os_addr, release)
            self.derive_support_status(os_addr, release)
            create_action_answer(action_node, release_node)

            self.logger.info(
                "OsSupportAgent: %s -> release %s, end of life %s",
                os_idtf, release.get("cycle"), release.get("eol"))
            return ScResult.OK
        except Exception as error:  # noqa: BLE001 - агент не должен ронять программу обработки
            self.logger.info("OsSupportAgent: finished with an error: %s", error)
            return ScResult.ERROR

    # ------------------------------------------------------------------ #
    # Процедура 2. Извлечение сущности, выделенной в сообщении            #
    # ------------------------------------------------------------------ #
    def get_entity(self, message_addr: ScAddr) -> ScAddr:
        rrel_entity = ScKeynodes.resolve("rrel_entity", sc_types.NODE_ROLE)
        template = ScTemplate()
        template.triple_with_relation(
            message_addr,
            sc_types.EDGE_ACCESS_VAR_POS_PERM,
            sc_types.NODE_VAR,
            sc_types.EDGE_ACCESS_VAR_POS_PERM,
            rrel_entity,
        )
        results = template_search(template)
        return results[0][2] if results else ScAddr(0)

    # ------------------------------------------------------------------ #
    # Процедура 3. Запрос к внешнему сервису и разбор JSON                #
    # ------------------------------------------------------------------ #
    def fetch_release(self, product: str, cycle: str):
        """Вернуть описание выпуска: закреплённый цикл либо самый новый."""
        releases = self.get_releases(product)
        if not releases:
            return None
        if cycle:
            for release in releases:
                if str(release.get("cycle")) == cycle:
                    return release
            self.logger.info("OsSupportAgent: cycle %s not found for %s", cycle, product)
            return None
        return releases[0]

    def get_releases(self, product: str):
        """Получить список выпусков продукта: из кэша либо из внешнего сервиса."""
        cached = _RESPONSE_CACHE.get(product)
        now = datetime.datetime.now().timestamp()
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            self.logger.info("OsSupportAgent: using cached response for %s", product)
            return cached[1]
        try:
            response = requests.get(API_URL.format(product=product), timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as error:
            self.logger.info("OsSupportAgent: the source is unavailable: %s", error)
            return cached[1] if cached else None
        if response.status_code != 200:
            self.logger.info(
                "OsSupportAgent: the source answered %s for %s", response.status_code, product)
            return None
        releases = response.json()
        if releases:
            _RESPONSE_CACHE[product] = (now, releases)
        return releases

    # ------------------------------------------------------------------ #
    # Процедура 4. Погружение сведений о выпуске в базу знаний            #
    # ------------------------------------------------------------------ #
    def immerse_release(self, os_addr: ScAddr, release: dict) -> ScAddr:
        release_class = ScKeynodes.resolve(
            "concept_operating_system_release", sc_types.NODE_CONST_CLASS)
        release_node = create_node(sc_types.NODE_CONST)
        create_edge(sc_types.EDGE_ACCESS_CONST_POS_PERM, release_class, release_node)

        os_name = self.get_ru_main_idtf(os_addr) or get_system_idtf(os_addr)
        release_name = self.build_release_name(os_name, release)

        self.set_ru_link(release_node, "nrel_main_idtf", release_name)
        self.set_ru_link(release_node, "nrel_release_date",
                         self.format_date(release.get("releaseDate")))
        self.set_ru_link(release_node, "nrel_end_of_life_date",
                         self.format_date(release.get("eol")))
        if release.get("latest"):
            self.set_ru_link(release_node, "nrel_latest_version", str(release["latest"]))
        self.set_ru_link(release_node, "nrel_data_retrieval_date",
                         self.format_date(datetime.date.today().isoformat()))

        if release.get("lts"):
            lts_class = ScKeynodes.resolve("concept_lts_release", sc_types.NODE_CONST_CLASS)
            create_edge(sc_types.EDGE_ACCESS_CONST_POS_PERM, lts_class, release_node)

        self.set_norole_relation(os_addr, release_node, "nrel_actual_release")
        return release_node

    # ------------------------------------------------------------------ #
    # Процедура 5. Вывод нового знания на погружённых сведениях           #
    # ------------------------------------------------------------------ #
    def derive_support_status(self, os_addr: ScAddr, release: dict) -> None:
        """Отнести операционную систему к поддерживаемым либо к завершённым.

        Знание выводится не из внешнего источника напрямую, а из уже
        погружённой даты окончания поддержки в сравнении с текущей датой.
        """
        eol = release.get("eol")
        supported = (eol is False or eol is None
                     or str(eol) >= datetime.date.today().isoformat())
        status_idtf = ("concept_supported_operating_system" if supported
                       else "concept_unsupported_operating_system")
        status_class = ScKeynodes.resolve(status_idtf, sc_types.NODE_CONST_CLASS)
        create_edge(sc_types.EDGE_ACCESS_CONST_POS_PERM, status_class, os_addr)
        self.logger.info("OsSupportAgent: derived %s", status_idtf)

    # ------------------------------------------------------------------ #
    # Процедура 6. Работа с ранее погружёнными сведениями                 #
    # ------------------------------------------------------------------ #
    def has_cached_release(self, os_addr: ScAddr) -> bool:
        nrel = ScKeynodes.resolve("nrel_actual_release", sc_types.NODE_CONST_NOROLE)
        return get_element_by_norole_relation(src=os_addr, nrel_node=nrel).is_valid()

    def clear_previous_knowledge(self, os_addr: ScAddr) -> None:
        """Снять прошлый актуальный выпуск и прошлый статус поддержки."""
        nrel = ScKeynodes.resolve("nrel_actual_release", sc_types.NODE_CONST_NOROLE)
        template = ScTemplate()
        template.triple_with_relation(
            os_addr,
            sc_types.EDGE_D_COMMON_VAR,
            sc_types.NODE_VAR,
            sc_types.EDGE_ACCESS_VAR_POS_PERM,
            nrel,
        )
        for result in template_search(template):
            delete_edges(result[0], result[2], sc_types.EDGE_D_COMMON_VAR)

        for idtf in ("concept_supported_operating_system",
                     "concept_unsupported_operating_system"):
            status_class = ScKeynodes.resolve(idtf, sc_types.NODE_CONST_CLASS)
            delete_edges(status_class, os_addr, sc_types.EDGE_ACCESS_VAR_POS_PERM)

    # ------------------------------------------------------------------ #
    # Вспомогательные операции                                           #
    # ------------------------------------------------------------------ #
    def set_ru_link(self, src: ScAddr, nrel_idtf: str, content: str) -> ScAddr:
        """Создать русскоязычную sc-ссылку и связать её с узлом отношением."""
        link = create_link(content, ScLinkContentType.STRING, link_type=sc_types.LINK_CONST)
        lang_ru = ScKeynodes.resolve("lang_ru", sc_types.NODE_CONST_CLASS)
        create_edge(sc_types.EDGE_ACCESS_CONST_POS_PERM, lang_ru, link)
        self.set_norole_relation(src, link, nrel_idtf)
        return link

    def set_norole_relation(self, src: ScAddr, trg: ScAddr, nrel_idtf: str) -> None:
        nrel = ScKeynodes.resolve(nrel_idtf, sc_types.NODE_CONST_NOROLE)
        edge = create_edge(sc_types.EDGE_D_COMMON_CONST, src, trg)
        create_edge(sc_types.EDGE_ACCESS_CONST_POS_PERM, nrel, edge)

    def get_link_value(self, src: ScAddr, nrel_idtf: str):
        nrel = ScKeynodes.resolve(nrel_idtf, sc_types.NODE_CONST_NOROLE)
        link = get_element_by_norole_relation(src=src, nrel_node=nrel)
        if not link.is_valid():
            return None
        return get_link_content_data(link)

    def get_ru_main_idtf(self, addr: ScAddr):
        main_idtf = ScKeynodes.resolve("nrel_main_idtf", sc_types.NODE_CONST_NOROLE)
        lang_ru = ScKeynodes.resolve("lang_ru", sc_types.NODE_CONST_CLASS)
        template = ScTemplate()
        template.triple_with_relation(
            addr,
            sc_types.EDGE_D_COMMON_VAR,
            sc_types.LINK,
            sc_types.EDGE_ACCESS_VAR_POS_PERM,
            main_idtf,
        )
        for result in template_search(template):
            link = result[2]
            if check_edge(sc_types.EDGE_ACCESS_VAR_POS_PERM, lang_ru, link):
                return get_link_content_data(link)
        return None

    @staticmethod
    def build_release_name(os_name: str, release: dict) -> str:
        """Собрать читаемое название выпуска, не удваивая номер версии.

        «Windows 7» + метка «7 SP1» дают «Windows 7 SP1», а не «Windows 7 7 SP1».
        """
        label = str(release.get("releaseLabel") or release.get("cycle") or "").strip()
        os_words = os_name.split()
        label_words = label.split()
        if os_words and label_words and os_words[-1].lower() == label_words[0].lower():
            label_words = label_words[1:]
        name = " ".join([os_name] + label_words).strip()
        codename = release.get("codename")
        if codename:
            name = f"{name} «{codename}»"
        return name

    @staticmethod
    def format_date(value) -> str:
        """Привести дату вида 2031-05-29 к виду «29 мая 2031 года»."""
        if not value or value is True:
            return EOL_NOT_ANNOUNCED
        try:
            date = datetime.date.fromisoformat(str(value))
        except ValueError:
            return str(value)
        return f"{date.day} {MONTHS[date.month - 1]} {date.year} года"
