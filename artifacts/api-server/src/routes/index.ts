import { Router, type IRouter } from "express";
import healthRouter from "./health";
import botStatsRouter from "./bot-stats";

const router: IRouter = Router();

router.use(healthRouter);
router.use(botStatsRouter);

export default router;
