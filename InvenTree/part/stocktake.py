"""Stocktake report functionality"""

import io
import logging
import time
from datetime import datetime

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.utils.translation import gettext_lazy as _

import tablib
from djmoney.contrib.exchange.exceptions import MissingRate
from djmoney.contrib.exchange.models import convert_money
from djmoney.money import Money

import common.models
import InvenTree.helpers
import part.models
import stock.models

logger = logging.getLogger('inventree')


def perform_stocktake(target: part.models.Part, user: User, note: str = '', commit=True, **kwargs):
    """Perform stocktake action on a single part.

    arguments:
        target: A single Part model instance
        commit: If True (default) save the result to the database
        user: User who requested this stocktake

    kwargs:
        exclude_external: If True, exclude stock items in external locations (default = False)

    Returns:
        PartStocktake: A new PartStocktake model instance (for the specified Part)
    """

    # Grab all "available" stock items for the Part
    # We do not include variant stock when performing a stocktake,
    # otherwise the stocktake entries will be duplicated
    stock_entries = target.stock_entries(in_stock=True, include_variants=False)

    exclude_external = kwargs.get('exclude_external', False)

    if exclude_external:
        stock_entries = stock_entries.exclude(location__external=True)

    # Cache min/max pricing information for this Part
    pricing = target.pricing

    if not pricing.is_valid:
        # If pricing is not valid, let's update
        logger.info(f"Pricing not valid for {target} - updating")
        pricing.update_pricing(cascade=False)
        pricing.refresh_from_db()

    base_currency = common.settings.currency_code_default()

    total_quantity = 0
    total_cost_min = Money(0, base_currency)
    total_cost_max = Money(0, base_currency)

    for entry in stock_entries:

        # Update total quantity value
        total_quantity += entry.quantity

        has_pricing = False

        # Update price range values
        if entry.purchase_price:
            # If purchase price is available, use that
            try:
                pp = convert_money(entry.purchase_price, base_currency) * entry.quantity
                total_cost_min += pp
                total_cost_max += pp
                has_pricing = True
            except MissingRate:
                logger.warning(f"MissingRate exception occurred converting {entry.purchase_price} to {base_currency}")

        if not has_pricing:
            # Fall back to the part pricing data
            p_min = pricing.overall_min or pricing.overall_max
            p_max = pricing.overall_max or pricing.overall_min

            if p_min or p_max:
                try:
                    total_cost_min += convert_money(p_min, base_currency) * entry.quantity
                    total_cost_max += convert_money(p_max, base_currency) * entry.quantity
                except MissingRate:
                    logger.warning(f"MissingRate exception occurred converting {p_min}:{p_max} to {base_currency}")

    # Construct PartStocktake instance
    instance = part.models.PartStocktake(
        part=target,
        item_count=stock_entries.count(),
        quantity=total_quantity,
        cost_min=total_cost_min,
        cost_max=total_cost_max,
        note=note,
        user=user,
    )

    if commit:
        instance.save()

    return instance


def generate_stocktake_report(**kwargs):
    """Generated a new stocktake report.

    Note that this method should be called only by the background worker process!

    Unless otherwise specified, the stocktake report is generated for *all* Part instances.
    Optional filters can by supplied via the kwargs

    kwargs:
        user: The user who requested this stocktake (set to None for automated stocktake)
        part: Optional Part instance to filter by (including variant parts)
        category: Optional PartCategory to filter results
        location: Optional StockLocation to filter results
        exclude_external: If True, exclude stock items in external locations (default = False)
        generate_report: If True, generate a stocktake report from the calculated data (default=True)
        update_parts: If True, save stocktake information against each filtered Part (default = True)
    """

    # Determine if external locations should be excluded
    exclude_external = kwargs.get(
        'exclude_exernal',
        common.models.InvenTreeSetting.get_setting('STOCKTAKE_EXCLUDE_EXTERNAL', False)
    )

    parts = part.models.Part.objects.all()
    user = kwargs.get('user', None)

    generate_report = kwargs.get('generate_report', True)
    update_parts = kwargs.get('update_parts', True)

    # Filter by 'Part' instance
    if p := kwargs.get('part', None):
        variants = p.get_descendants(include_self=True)
        parts = parts.filter(
            pk__in=[v.pk for v in variants]
        )

    # Filter by 'Category' instance (cascading)
    if category := kwargs.get('category', None):
        categories = category.get_descendants(include_self=True)
        parts = parts.filter(category__in=categories)

    # Filter by 'Location' instance (cascading)
    # Stocktake report will be limited to parts which have stock items within this location
    if location := kwargs.get('location', None):
        # Extract flat list of all sublocations
        locations = list(location.get_descendants(include_self=True))

        # Items which exist within these locations
        items = stock.models.StockItem.objects.filter(location__in=locations)

        if exclude_external:
            items = items.exclude(location__external=True)

        # List of parts which exist within these locations
        unique_parts = items.order_by().values('part').distinct()

        parts = parts.filter(
            pk__in=[result['part'] for result in unique_parts]
        )

    # Exit if filters removed all parts
    n_parts = parts.count()

    if n_parts == 0:
        logger.info("No parts selected for stocktake report - exiting")
        return

    logger.info(f"Generating new stocktake report for {n_parts} parts")

    base_currency = common.settings.currency_code_default()

    # Construct an initial dataset for the stocktake report
    dataset = tablib.Dataset(
        headers=[
            _('Part ID'),
            _('Part Name'),
            _('Part Description'),
            _('Category ID'),
            _('Category Name'),
            _('Stock Items'),
            _('Total Quantity'),
            _('Total Cost Min') + f' ({base_currency})',
            _('Total Cost Max') + f' ({base_currency})',
        ]
    )

    parts = parts.prefetch_related('category', 'stock_items')

    # Simple profiling for this task
    t_start = time.time()

    # Keep track of each individual "stocktake" we perform.
    # They may be bulk-commited to the database afterwards
    stocktake_instances = []

    total_parts = 0

    # Iterate through each Part which matches the filters above
    for p in parts:

        # Create a new stocktake for this part (do not commit, this will take place later on)
        stocktake = perform_stocktake(p, user, commit=False, exclude_external=exclude_external)

        if stocktake.quantity == 0:
            # Skip rows with zero total quantity
            continue

        total_parts += 1

        stocktake_instances.append(stocktake)

        # Add a row to the dataset
        dataset.append([
            p.pk,
            p.full_name,
            p.description,
            p.category.pk if p.category else '',
            p.category.name if p.category else '',
            stocktake.item_count,
            stocktake.quantity,
            InvenTree.helpers.normalize(stocktake.cost_min.amount),
            InvenTree.helpers.normalize(stocktake.cost_max.amount),
        ])

    # Save a new PartStocktakeReport instance
    buffer = io.StringIO()
    buffer.write(dataset.export('csv'))

    today = datetime.now().date().isoformat()
    filename = f"InvenTree_Stocktake_{today}.csv"
    report_file = ContentFile(buffer.getvalue(), name=filename)

    if generate_report:
        report_instance = part.models.PartStocktakeReport.objects.create(
            report=report_file,
            part_count=total_parts,
            user=user
        )

        # Notify the requesting user
        if user:

            common.notifications.trigger_notification(
                report_instance,
                category='generate_stocktake_report',
                context={
                    'name': _('Stocktake Report Available'),
                    'message': _('A new stocktake report is available for download'),
                },
                targets=[
                    user,
                ]
            )

    # If 'update_parts' is set, we save stocktake entries for each individual part
    if update_parts:
        # Use bulk_create for efficient insertion of stocktake
        part.models.PartStocktake.objects.bulk_create(
            stocktake_instances,
            batch_size=500,
        )

    t_stocktake = time.time() - t_start
    logger.info(f"Generated stocktake report for {total_parts} parts in {round(t_stocktake, 2)}s")
