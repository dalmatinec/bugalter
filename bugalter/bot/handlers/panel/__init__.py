"""Панель управления на кнопках: /admin или кнопка Управление в личке с ботом.

Каждый раздел панели в своём модуле; здесь они собираются в один роутер.
"""

from aiogram import Router

from . import (
    access,
    backups,
    coefficients,
    departments,
    employees,
    forms,
    groups,
    home,
    journal,
    payouts,
    products,
    settings,
    stock,
    works,
)
from .base import can_admin
from .employees import accept_invite_start

router = Router(name="panel")
# forms последним: кнопки и команды разделов должны срабатывать раньше, чем текст уйдёт ответом на вопрос.
router.include_routers(
    home.router,
    employees.router,
    payouts.router,
    access.router,
    works.router,
    coefficients.router,
    products.router,
    stock.router,
    groups.router,
    settings.router,
    backups.router,
    departments.router,
    journal.router,
    forms.router,
)

__all__ = ["accept_invite_start", "can_admin", "router"]
