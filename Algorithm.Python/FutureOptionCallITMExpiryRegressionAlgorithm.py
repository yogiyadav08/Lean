from datetime import datetime, timedelta

import clr
from System import *
from System.Reflection import *
from QuantConnect import *
from QuantConnect.Algorithm import *
from QuantConnect.Data import *
from QuantConnect.Data.Market import *
from QuantConnect.Orders import *
from QuantConnect.Securities import *
from QuantConnect.Securities.Future import *
from QuantConnect import Market


class FutureOptionCallITMExpiryRegressionAlgorithm(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate(2020, 9, 22)
        clr.GetClrType(QCAlgorithm).GetField("_endDate", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(self, DateTime(2021, 3, 30))

        # We add AAPL as a temporary workaround for https://github.com/QuantConnect/Lean/issues/4872
        # which causes delisting events to never be processed, thus leading to options that might never
        # be exercised until the next data point arrives.
        self.AddEquity("AAPL", Resolution.Daily)

        self.es19h21 = self.AddFutureContract(
            Symbol.CreateFuture(
                Futures.Indices.SP500EMini,
                Market.CME,
                datetime(2021, 3, 19)
            ),
            Resolution.Minute).Symbol

        # Select a future option expiring ITM, and adds it to the algorithm.
        self.esOption = self.AddFutureOptionContract(
            list(
                sorted([x for x in self.OptionChainProvider.GetOptionContractList(self.es19h21, self.Time) if x.ID.StrikePrice <= 3250.0], key=lambda x: x.ID.StrikePrice, reverse=True)
            )[0], Resolution.Minute).Symbol

        self.expectedContract = Symbol.CreateOption(self.es19h21, Market.CME, OptionStyle.American, OptionRight.Call, 3250.0, datetime(2021, 3, 19))
        if self.esOption != self.expectedContract:
            raise Exception(f"Contract {self.expectedContract} was not found in the chain")

        self.Schedule.On(self.DateRules.Today, self.TimeRules.AfterMarketOpen(self.es19h21, 1), self.ScheduleCallback)

    def ScheduleCallback(self):
        self.MarketOrder(self.esOption, 1)

    def OnData(self, data: Slice):
        # Assert delistings, so that we can make sure that we receive the delisting warnings at
        # the expected time. These assertions detect bug #4872
        for delisting in data.Delistings.Values:
            if delisting.Type == DelistingType.Warning:
                if delisting.Time != datetime(2021, 3, 19):
                    raise Exception(f"Delisting warning issued at unexpected date: {delisting.Time}")
            elif delisting.Type == DelistingType.Delisted:
                if delisting.Time != datetime(2021, 3, 20):
                    raise Exception(f"Delisting happened at unexpected date: {delisting.Time}")

    def OnOrderEvent(self, orderEvent: OrderEvent):
        if orderEvent.Status != OrderStatus.Filled:
            # There's lots of noise with OnOrderEvent, but we're only interested in fills.
            return

        if not self.Securities.ContainsKey(orderEvent.Symbol):
            raise Exception(f"Order event Symbol not found in Securities collection: {orderEvent.Symbol}")

        security = self.Securities[orderEvent.Symbol]
        if security.Symbol == self.es19h21:
            self.AssertFutureOptionOrderExercise(orderEvent, security, self.Securities[self.expectedContract])
        elif security.Symbol == self.expectedContract:
            # Expected contract is ES19H21 Call Option expiring ITM @ 3250
            self.AssertFutureOptionContractOrder(orderEvent, security)
        else:
            raise Exception(f"Received order event for unknown Symbol: {orderEvent.Symbol}")

        self.Log(f"{self.Time} -- {orderEvent.Symbol} :: Price: {self.Securities[orderEvent.Symbol].Holdings.Price} Qty: {self.Securities[orderEvent.Symbol].Holdings.Quantity} Direction: {orderEvent.Direction} Msg: {orderEvent.Message}")

    def AssertFutureOptionOrderExercise(self, orderEvent: OrderEvent, future: Security, optionContract: Security):
        # This vvvv is the actual expected liquidation date. But because of the issue described above w/ FillForward,
        # we will modify the liquidation date to 2021-03-22 (Monday) since that's when equities start trading again.
        # `datetime(2021, 3, 19, 5, 0, 0)`
        expectedLiquidationTimeUtc = datetime(2021, 3, 22, 13, 32, 0)

        if orderEvent.Direction == OrderDirection.Sell and future.Holdings.Quantity != 0:
            # We expect the contract to have been liquidated immediately
            raise Exception(f"Did not liquidate existing holdings for Symbol {future.Symbol}")
        if orderEvent.Direction == OrderDirection.Sell and orderEvent.UtcTime.replace(tzinfo=None) != expectedLiquidationTimeUtc:
            raise Exception(f"Liquidated future contract, but not at the expected time. Expected: {expectedLiquidationTimeUtc} - found {orderEvent.UtcTime.replace(tzinfo=None)}");

        # No way to detect option exercise orders or any other kind of special orders
        # other than matching strings, for now.
        if "Option Exercise" in orderEvent.Message:
            if future.Holdings.Quantity != 1:
                # Here, we expect to have some holdings in the underlying, but not in the future option anymore.
                raise Exception(f"Exercised option contract, but we have no holdings for Future {future.Symbol}")

            if optionContract.Holdings.Quantity != 0:
                raise Exception(f"Exercised option contract, but we have holdings for Option contract {optionContract.Symbol}")

    def AssertFutureOptionContractOrder(self, orderEvent: OrderEvent, option: Security):
        if orderEvent.Direction == OrderDirection.Buy and option.Holdings.Quantity != 1:
            raise Exception(f"No holdings were created for option contract {option.Symbol}")

        if orderEvent.Direction == OrderDirection.Sell and option.Holdings.Quantity != 0:
            raise Exception(f"Holdings were found after a filled option exercise")

        if "Exercise" in orderEvent.Message and option.Holdings.Quantity != 0:
            raise Exception(f"Holdings were found after exercising option contract {option.Symbol}")
    